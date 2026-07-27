"""Who is acting, and may they act — the admin listener's two guards.

Both are FastAPI dependencies so 5.5.6 (WebAuthn) is a change to THIS
file and nowhere else: `current_operator` starts returning the verified
credential's identity, and every route that already depends on it
starts recording a cryptographically-established human.

The rule these encode: **identity is resolved by the server, never
asserted by the caller.** An actor field in a request body is a
signature anyone can type — and this API's actor fields end up in the
audit log, which is supposed to be the canonical record of who
approved what.
"""

from fastapi import HTTPException, Request

from .config import OPERATOR

# Requiring a non-standard header on every state-changing call is the
# third, independent CSRF layer (after Host and Origin checks). A
# cross-origin page cannot send a custom header without a CORS
# preflight, and this app answers no preflight and sets no CORS
# headers — so the browser refuses before Sentinel is ever asked.
CONSOLE_HEADER = "x-sentinel-console"


def current_operator() -> str:
    """The human the audit log will name. 5.5.6 swaps the source for a
    verified passkey; callers of this function do not change."""
    return OPERATOR


def console_guard(request: Request) -> None:
    """Gate for grant / deny / kill / release. Not authentication —
    that is 5.5.6 — but the control that stops a *web page the operator
    happens to visit* from driving the console behind their back."""
    if request.headers.get(CONSOLE_HEADER) != "1":
        raise HTTPException(
            status_code=403,
            detail=f"state-changing calls must send '{CONSOLE_HEADER}: 1' "
                   "(CSRF guard — the console sends it automatically; "
                   "add -H '" + CONSOLE_HEADER + ": 1' to curl)",
        )
