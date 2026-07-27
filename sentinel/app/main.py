"""Sentinel Admin — the HUMAN-FACING listener (console + curl).

TRUST-DOMAIN RULE (CLAUDE.md "Trust-domain separation is
non-negotiable"): this app binds to 127.0.0.1 ONLY. Cluster pods reach
the WSL2 host via the k3d network gateway address, which can never
address the host's loopback — so everything here (grant, deny, kill,
audit) is unreachable from inside k3d by construction, before any auth
exists. The cluster-facing surface lives in app.broker — never mount
granting routes there.

Loopback is not, however, the same as safe. The console is a WEB page
in the operator's browser, and a browser will happily carry a request
from any other tab. So three independent controls sit in front of
every state-changing route, none of which is authentication (that is
5.5.6, and it stacks on top):

  1. Host allowlist   — defeats DNS rebinding (`Host: evil.com` → 403)
  2. Origin check     — defeats plain cross-site requests
  3. Console header   — a custom header forces a CORS preflight that
                        this app never answers, so the browser refuses
                        before Sentinel is asked

And the actor is resolved server-side (app.actor), so the audit log
records who Sentinel BELIEVES acted rather than a name the caller
typed.

Console: http://127.0.0.1:8400/     Interactive API docs: /docs
"""

from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .actor import console_guard, current_operator
from .config import CONSOLE_ALLOWED_HOSTS, FLOW_ACTIVE_MINUTES
from .db import SessionLocal, engine
from .models import (
    AuditEvent,
    CapabilityGrant,
    CapabilityRequest,
    Flow,
    RequestStatus,
    utcnow,
)
from .schemas import (
    AuditEventOut,
    DenyIn,
    FlowOut,
    GrantIn,
    GrantOut,
    KillIn,
    KillStatus,
    PendingRequest,
)
from .service import (
    deny_request,
    engage_kill,
    grant_request,
    kill_state,
    refresh_status,
    release_kill,
)

CONSOLE_DIR = Path(__file__).parent / "console"

# Everything the page needs comes from this origin; nothing else may be
# loaded, connected to, or framed. A trust anchor does not fetch code
# from the internet — that is also why there is no CDN in the console.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

app = FastAPI(
    title="Sentinel Admin (loopback-only)",
    version=__version__,
    description="The human's side of the broker: see what's asking, "
                "grant or deny, pull the kill switch, read the record.",
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=CONSOLE_ALLOWED_HOSTS)


@app.middleware("http")
async def _console_hardening(request: Request, call_next):
    """Layer 2 (Origin) + the response security headers."""
    origin = request.headers.get("origin")
    if origin is not None and urlparse(origin).hostname not in CONSOLE_ALLOWED_HOSTS:
        return JSONResponse(
            status_code=403,
            content={"detail": "cross-origin request refused"},
        )
    response = await call_next(request)
    response.headers["content-security-policy"] = CSP
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["cache-control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=CONSOLE_DIR), name="static")


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(CONSOLE_DIR / "index.html")


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "listener": "admin", "version": __version__,
            "operator": current_operator()}


def _get_request(s, request_id: str) -> CapabilityRequest:
    req = s.get(CapabilityRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown request_id")
    return req


@app.get(
    "/v1/capability-requests",
    response_model=list[PendingRequest],
    tags=["decisions"],
    summary="What is asking for power right now",
)
def list_pending():
    """The console's main panel (and the curl equivalent). Only pending
    requests appear; expiry is applied on read."""
    with SessionLocal() as s:
        rows = s.scalars(
            select(CapabilityRequest)
            .where(CapabilityRequest.status == RequestStatus.PENDING)
            .order_by(CapabilityRequest.requested_at)
        ).all()
        return [
            PendingRequest(
                request_id=r.id, flow_id=r.flow_id, agent=r.flow.agent,
                tool=r.tool, reason=r.reason,
                requested_at=r.requested_at, expires_at=r.expires_at,
            )
            for r in rows
            if refresh_status(s, r) == RequestStatus.PENDING
        ]


@app.post(
    "/v1/capability-requests/{request_id}/grant",
    response_model=GrantOut,
    status_code=201,
    tags=["decisions"],
    summary="Say yes — mint a scope-locked, short-lived token",
    dependencies=[Depends(console_guard)],
    responses={409: {"description": "Request no longer pending, or kill engaged."}},
)
def grant(request_id: str, body: GrantIn, operator: str = Depends(current_operator)):
    """Mints the token and hands it to the REQUESTER via its poll —
    deliberately never echoed here: the human approves power, they
    don't hold it. 409 if the request already resolved or the kill
    switch is engaged (no new grants while killed)."""
    with SessionLocal() as s:
        req = _get_request(s, request_id)
        try:
            g = grant_request(s, req, ttl_minutes=body.ttl_minutes,
                              granted_by=operator)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return GrantOut(request_id=req.id, status=req.status.value,
                        grant_id=g.id, expires_at=g.expires_at)


@app.post(
    "/v1/capability-requests/{request_id}/deny",
    tags=["decisions"],
    summary="Say no — resolves the requester's poll immediately",
    dependencies=[Depends(console_guard)],
    responses={409: {"description": "Request no longer pending."}},
)
def deny(request_id: str, body: DenyIn, operator: str = Depends(current_operator)):
    """Fail closed AND loud: the requester's next poll returns `denied`
    with your reason, instead of hanging until timeout."""
    with SessionLocal() as s:
        req = _get_request(s, request_id)
        try:
            deny_request(s, req, denied_by=operator, reason=body.reason)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"request_id": req.id, "status": req.status.value}


@app.get("/v1/kill", response_model=KillStatus, response_model_exclude_none=True,
         tags=["kill"], summary="Kill-switch state")
def kill_status():
    with SessionLocal() as s:
        return KillStatus.model_validate(kill_state(s))


@app.post(
    "/v1/kill",
    response_model=KillStatus,
    response_model_exclude_none=True,
    tags=["kill"],
    summary="ENGAGE: revoke every live grant, refuse all new ones",
    dependencies=[Depends(console_guard)],
)
def kill(body: KillIn, operator: str = Depends(current_operator)):
    """Kill is revocation, not pause: every live grant is permanently
    revoked (audited one by one) and /capability-check answers 403
    kill-engaged until released. The state persists in the DB, so a
    Sentinel restart while killed comes back killed. Idempotent."""
    with SessionLocal() as s:
        ks, revoked = engage_kill(s, by=operator, reason=body.reason)
        out = KillStatus.model_validate(ks)
        out.grants_revoked = revoked
        return out


@app.post(
    "/v1/kill/release",
    response_model=KillStatus,
    response_model_exclude_none=True,
    tags=["kill"],
    summary="RELEASE: new requests flow again (old tokens stay dead)",
    dependencies=[Depends(console_guard)],
)
def release(operator: str = Depends(current_operator)):
    """Releasing does NOT resurrect revoked grants — flows re-request
    and the human re-grants. Resumption is a fresh decision, on purpose."""
    with SessionLocal() as s:
        return KillStatus.model_validate(release_kill(s, by=operator))


@app.get(
    "/v1/audit-events",
    response_model=list[AuditEventOut],
    tags=["record"],
    summary="The canonical record, newest first",
)
def audit_events(
    limit: int = Query(default=50, ge=1, le=500),
    flow_id: str | None = Query(default=None, description="Filter to one flow."),
    event_type: str | None = Query(default=None, description="Filter to one type."),
):
    with SessionLocal() as s:
        q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
        if flow_id:
            q = q.where(AuditEvent.flow_id == flow_id)
        if event_type:
            q = q.where(AuditEvent.event_type == event_type)
        return [AuditEventOut.model_validate(e) for e in s.scalars(q).all()]


@app.get(
    "/v1/flows",
    response_model=list[FlowOut],
    tags=["record"],
    summary="Known flows, with the evidence for calling one active",
)
def flows(active: bool = Query(
    default=False,
    description="Only flows holding a live grant or seen within the "
                "activity window (SENTINEL_FLOW_ACTIVE_MINUTES).")):
    """Activity is DERIVED, never assumed: nothing closes a flow today,
    so `ended_at IS NULL` would mark every flow that ever ran as
    active — a console panel that grows forever and means nothing."""
    now = utcnow()
    with SessionLocal() as s:
        seen = dict(s.execute(
            select(AuditEvent.flow_id, func.max(AuditEvent.ts))
            .group_by(AuditEvent.flow_id)
        ).all())
        live = dict(s.execute(
            select(CapabilityGrant.flow_id, func.count())
            .where(CapabilityGrant.revoked_at.is_(None),
                   CapabilityGrant.expires_at > now)
            .group_by(CapabilityGrant.flow_id)
        ).all())
        waiting = dict(s.execute(
            select(CapabilityRequest.flow_id, func.count())
            .where(CapabilityRequest.status == RequestStatus.PENDING)
            .group_by(CapabilityRequest.flow_id)
        ).all())

        cutoff = now - timedelta(minutes=FLOW_ACTIVE_MINUTES)
        out = []
        for f in s.scalars(select(Flow).order_by(Flow.started_at.desc())).all():
            row = FlowOut(
                id=f.id, agent=f.agent, started_at=f.started_at,
                ended_at=f.ended_at, last_seen=seen.get(f.id),
                live_grants=live.get(f.id, 0),
                pending_requests=waiting.get(f.id, 0),
            )
            if active and (
                f.ended_at is not None
                or not (row.live_grants or (row.last_seen and row.last_seen >= cutoff))
            ):
                continue
            out.append(row)
        return out
