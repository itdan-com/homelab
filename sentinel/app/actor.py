"""Who is acting, and may they act — the admin listener's guards.

The rule these encode: **identity is resolved by the server, never
asserted by the caller.** An actor field in a request body is a
signature anyone can type — and this API's actor fields end up in the
audit log, which is supposed to be the canonical record of who
approved what.

As of 5.5.6 that identity is a WebAuthn credential the human physically
holds (see `app.auth`), not a config string. `SENTINEL_OPERATOR`
survives only as the name given to the *first* enrolled operator by
`scripts/enroll-operator.sh`; nothing authenticates with it.
"""

from fastapi import Depends, HTTPException, Request

from .auth import SESSION_COOKIE, session_operator
from .db import SessionLocal
from .models import Operator

# Requiring a non-standard header on every state-changing call is one of
# three CSRF layers (with the Host allowlist and the Origin check). A
# cross-origin page cannot send a custom header without a CORS
# preflight, and this app answers no preflight and sets no CORS
# headers — so the browser refuses before Sentinel is ever asked.
CONSOLE_HEADER = "x-sentinel-console"


def console_guard(request: Request) -> None:
    """CSRF gate for every state-changing route. Not authentication —
    that is `require_operator` — but the control that stops a *web page
    the operator happens to visit* from driving the console behind
    their back, including while they hold a valid session."""
    if request.headers.get(CONSOLE_HEADER) != "1":
        raise HTTPException(
            status_code=403,
            detail=f"state-changing calls must send '{CONSOLE_HEADER}: 1' "
                   "(CSRF guard — the console sends it automatically; "
                   "add -H '" + CONSOLE_HEADER + ": 1' to curl)",
        )


def require_operator(request: Request) -> Operator:
    """The authenticated human, or 401. Every route that decides
    something — grant, deny, kill, release — depends on this, so an
    unauthenticated console can look at nothing and change nothing."""
    with SessionLocal() as s:
        operator = session_operator(s, request.cookies.get(SESSION_COOKIE))
        if operator is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        # Detached copy: the caller uses it after this session closes.
        return Operator(id=operator.id, username=operator.username,
                        created_at=operator.created_at)


def current_operator(operator: Operator = Depends(require_operator)) -> str:
    """The name the audit log will record — now a cryptographically
    established human rather than a configured string."""
    return operator.username
