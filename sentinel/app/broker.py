"""Sentinel Broker — the CLUSTER-FACING listener.

This is the only Sentinel surface pods may reach. Run it bound to the
k3d docker-network gateway address (the WSL host as pods see it):

    uvicorn app.broker:app --host <k3d-gateway-ip> --port 8401

It exposes exactly three operations — ask, poll, check — and can no
more grant itself a capability than any other caller: granting lives
on the admin listener, which binds loopback and is unreachable from
the cluster by construction (CLAUDE.md: one-way trust). mTLS in front
of this listener arrives with the Envoy ext_authz proxy work (5.5.4).
"""

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from . import __version__
from .db import SessionLocal, engine
from .models import RequestStatus
from .schemas import (
    CapabilityRequestIn,
    CapabilityRequestOut,
    CapabilityRequestStatus,
    CheckAllowed,
    CheckDenied,
    FLOW_ID_PATTERN,
    TOOL_PATTERN,
)
from .service import check_capability, claim_token, create_request, refresh_status

app = FastAPI(
    title="Sentinel Broker (cluster-facing)",
    version=__version__,
    description="Ask for a capability, poll for the human's answer, "
                "and let the proxy validate tokens. Nothing here grants.",
)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "listener": "broker", "version": __version__}


@app.post(
    "/v1/capability-requests",
    response_model=CapabilityRequestOut,
    status_code=202,
    tags=["capability"],
    summary="Ask the human for a capability",
    responses={200: {"description": "An identical request was already pending — "
                                    "returned instead of duplicated (retry-safe)."}},
)
def request_capability(body: CapabilityRequestIn, response: Response):
    """Registers the flow on first sight, records the request, and puts
    it in front of the human. **202** = new request created; **200** =
    you (or a retry of you) already asked and it is still pending —
    same body either way. Then poll `GET /v1/capability-requests/{id}`
    every few seconds until it resolves."""
    with SessionLocal() as s:
        req, created = create_request(
            s, flow_id=body.flow_id, tool=body.tool,
            reason=body.reason, agent=body.agent,
        )
        if not created:
            response.status_code = 200
        return CapabilityRequestOut(
            request_id=req.id, status=req.status.value, flow_id=req.flow_id,
            tool=req.tool, requested_at=req.requested_at, expires_at=req.expires_at,
        )


@app.get(
    "/v1/capability-requests/{request_id}",
    response_model=CapabilityRequestStatus,
    response_model_exclude_none=True,
    tags=["capability"],
    summary="Poll for the outcome (token arrives here, once)",
)
def poll_request(request_id: str):
    """`pending` → keep polling (the request itself expires, so this
    always terminates). `granted` → the **first** poll carries `token`
    (claim-once; later polls return the status without it — if the
    token never reached you, request again). `denied` / `expired` →
    stop and fail your action closed."""
    from .models import CapabilityRequest
    with SessionLocal() as s:
        req = s.get(CapabilityRequest, request_id)
        if req is None:
            raise HTTPException(status_code=404, detail="unknown request_id")
        status = refresh_status(s, req)
        token = claim_token(s, req) if status == RequestStatus.GRANTED else None
        return CapabilityRequestStatus(
            request_id=req.id, status=status.value, flow_id=req.flow_id,
            tool=req.tool, requested_at=req.requested_at, expires_at=req.expires_at,
            token=token,
            grant_expires_at=req.grant.expires_at if req.grant else None,
            denied_reason=req.denied_reason,
        )


@app.get(
    "/v1/capability-check",
    response_model=CheckAllowed,
    tags=["capability"],
    summary="Validate a token (the proxy's hot path)",
    responses={403: {"model": CheckDenied,
                     "description": "Denied — reason in body, and audited."}},
)
def capability_check(
    token: str = Query(description="The capability token, exactly as claimed."),
    tool: str = Query(pattern=TOOL_PATTERN,
                      description="Tool actually being invoked — must equal the granted tool."),
    flow_id: str = Query(pattern=FLOW_ID_PATTERN,
                         description="Flow actually invoking — must equal the granted flow."),
):
    """**HTTP status IS the verdict** — 200 allow, 403 deny — because
    that is the contract Envoy's `ext_authz` HTTP service speaks
    (5.5.4 plugs this in unchanged). Deny reasons are specific
    (unknown-token / scope-mismatch / revoked / expired / kill-engaged)
    because the caller is our own proxy and debuggability wins; every
    check, either verdict, lands in the audit log."""
    with SessionLocal() as s:
        allowed, reason, grant = check_capability(s, token=token, tool=tool, flow_id=flow_id)
        if not allowed:
            return JSONResponse(status_code=403,
                                content={"allowed": False, "reason": reason})
        return CheckAllowed(
            allowed=True, grant_id=grant.id, flow_id=grant.flow_id,
            tool=grant.tool, expires_at=grant.expires_at,
        )
