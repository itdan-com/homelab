"""Console auth endpoints (5.5.6), mounted on the admin listener.

Read-only routes are gated too: the pending panel and the audit log
describe what the platform's agent is trying to do and what it has
done, which is not public information just because it is not a button.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from . import auth
from .actor import console_guard, require_operator
from .config import SESSION_TTL_MINUTES
from .db import SessionLocal
from .models import Operator, WebAuthnCredential

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        max_age=SESSION_TTL_MINUTES * 60,
        httponly=True,      # JavaScript can never read it, so XSS cannot lift it
        samesite="strict",  # never sent on a cross-site navigation
        secure=True,        # http://localhost counts as a secure context
        path="/",
    )


@router.get("/status", summary="Is anyone enrolled, and am I signed in?")
def status(request: Request):
    """Drives the console's first paint: an empty install shows the
    enrollment instructions, a signed-out one shows the passkey prompt."""
    with SessionLocal() as s:
        operator = auth.session_operator(s, request.cookies.get(auth.SESSION_COOKIE))
        return {
            "enrolled": auth.operator_count(s) > 0,
            "authenticated": operator is not None,
            "operator": operator.username if operator else None,
        }


@router.post("/register/begin", dependencies=[Depends(console_guard)],
             summary="Start enrolling an authenticator (needs a host-minted code)")
def register_begin(body: dict):
    with SessionLocal() as s:
        started = auth.begin_registration(s, body.get("code", ""))
        if started is None:
            # Same answer for expired, used, and never-existed: an
            # enrollment code oracle is a way to probe for a live one.
            raise HTTPException(status_code=403, detail="invalid or expired enrollment code")
        return started


@router.post("/register/complete", dependencies=[Depends(console_guard)],
             summary="Finish enrolling; the passkey now exists")
def register_complete(body: dict):
    with SessionLocal() as s:
        operator = auth.finish_registration(s, body)
        if operator is None:
            raise HTTPException(status_code=403, detail="registration failed")
        return {"registered": True, "operator": operator.username}


@router.post("/login/begin", dependencies=[Depends(console_guard)],
             summary="Challenge for a passkey")
def login_begin():
    with SessionLocal() as s:
        return {"options": auth.begin_login(s)}


@router.post("/login/complete", dependencies=[Depends(console_guard)],
             summary="Verify the passkey and open a session")
def login_complete(body: dict, response: Response):
    with SessionLocal() as s:
        result = auth.finish_login(s, body)
        if result is None:
            raise HTTPException(status_code=401, detail="authentication failed")
        operator, token = result
        _set_session_cookie(response, token)
        return {"authenticated": True, "operator": operator.username}


@router.post("/login/totp", dependencies=[Depends(console_guard)],
             summary="Fallback: verify a TOTP code and open a session")
def login_totp(body: dict, response: Response):
    """The documented fallback for when no authenticator is to hand. It
    is second by design — a TOTP seed is a shared secret that can be
    phished, which is the thing the passkey exists to prevent."""
    with SessionLocal() as s:
        result = auth.login_with_totp(s, body.get("username", ""), body.get("code", ""))
        if result is None:
            raise HTTPException(status_code=401, detail="authentication failed")
        operator, token = result
        _set_session_cookie(response, token)
        return {"authenticated": True, "operator": operator.username}


@router.post("/logout", dependencies=[Depends(console_guard)])
def logout(request: Request, response: Response):
    with SessionLocal() as s:
        auth.close_session(s, request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"authenticated": False}


@router.get("/credentials", summary="Authenticators registered to me")
def credentials(operator: Operator = Depends(require_operator)):
    from sqlalchemy import select
    with SessionLocal() as s:
        rows = s.scalars(select(WebAuthnCredential).where(
            WebAuthnCredential.operator_id == operator.id)).all()
        return [{"id": c.id, "label": c.label, "created_at": c.created_at,
                 "last_used_at": c.last_used_at} for c in rows]


@router.post("/totp/enroll", dependencies=[Depends(console_guard)],
             summary="Generate a TOTP seed for the signed-in operator")
def totp_enroll(operator: Operator = Depends(require_operator)):
    """Only an already-authenticated operator can enrol the fallback:
    it is a convenience for someone who has already proved who they
    are, never a way in."""
    import base64
    import io

    import qrcode
    import qrcode.image.svg

    with SessionLocal() as s:
        row = s.get(Operator, operator.id)
        uri = auth.totp_provisioning_uri(s, row)

    # Rendered here rather than by a JavaScript QR library: the console
    # loads no third-party code, and a data: URI satisfies its CSP.
    # SVG rather than PNG so the trust anchor does not grow a Pillow
    # dependency for one picture — fewer packages in the service that
    # holds the kill switch is worth more than a raster image.
    buf = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return {"uri": uri,
            "qr_data_uri": "data:image/svg+xml;base64,"
                           + base64.b64encode(buf.getvalue()).decode()}


@router.post("/totp/confirm", dependencies=[Depends(console_guard)],
             summary="Prove the authenticator app has the seed")
def totp_confirm(body: dict, operator: Operator = Depends(require_operator)):
    with SessionLocal() as s:
        row = s.get(Operator, operator.id)
        if not auth.confirm_totp(s, row, body.get("code", "")):
            raise HTTPException(status_code=400, detail="code did not verify")
        return {"confirmed": True}
