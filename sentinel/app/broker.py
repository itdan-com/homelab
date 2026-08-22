"""Sentinel Broker — the CLUSTER-FACING listener.

This is the only Sentinel surface pods may reach. Run it bound to the
k3d docker-network gateway address (the WSL host as pods see it):

    uvicorn app.broker:app --host <k3d-gateway-ip> --port 8401

It exposes ask, poll, check, and the ext_authz entry point — and can
no more grant itself a capability than any other caller: granting
lives on the admin listener, which binds loopback and is unreachable
from the cluster by construction (CLAUDE.md: one-way trust). This
listener serves **mTLS** (5.5.4): scripts/run-broker.sh requires a
client certificate from Sentinel's OWN CA (scripts/mint-certs.sh) —
holding one is the price of talking to the broker at all. The
in-cluster enforcement point in front of MCP servers is
catalog/sentinel-proxy: Envoy's ext_authz filter asking /v1/ext-authz
about every request.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from . import policy
from .config import POLICY_RELOAD_SECONDS
from .db import SessionLocal, engine
from .models import AuditEventType, RequestStatus
from .schemas import (
    CapabilityRequestIn,
    CapabilityRequestOut,
    CapabilityRequestStatus,
    CheckAllowed,
    CheckDenied,
    FLOW_ID_PATTERN,
    TOOL_PATTERN,
)
from .scope import derive_scope
from .service import (
    audit,
    check_capability,
    claim_token,
    create_request,
    nonce_matches,
    refresh_status,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """7.3.1: the broker loads the policy store at startup and watches
    it for console activations — READ-ONLY both times (policy.refresh);
    the console, in the admin process, is the store's only writer.
    Failure must not stop the broker: its 5.5 duties (tokens, proxy
    checks) need no store, and the person path denies closed while
    nothing is active. POLICY_DIR is passed explicitly so the value is
    read at call time, not frozen as a default argument at import."""
    log = logging.getLogger("sentinel")
    try:
        ap = policy.refresh(policy.POLICY_DIR)
        log.info("policy store active: %s", ap.version)
    except Exception as e:  # PolicyError, missing store, …
        log.warning("policy store not active: %s", e)
    task = (asyncio.create_task(
                policy.watch_store(policy.POLICY_DIR, POLICY_RELOAD_SECONDS))
            if POLICY_RELOAD_SECONDS > 0 else None)
    yield
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    lifespan=lifespan,
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
    ap = policy.get_active()
    return {"status": "ok", "listener": "broker", "version": __version__,
            "policy_version": ap.version if ap else None}


@app.get("/metrics", tags=["meta"])
def metrics() -> Response:
    """ADR-006 Decision 1: rates and state for Prometheus — read-only,
    bounded labels only (never principal or resource). mTLS is the whole
    gate, as for every broker route: holding a Sentinel-CA client cert
    is the price of scraping, and the endpoint grants nothing."""
    from . import metrics as m
    with SessionLocal() as s:
        body = m.render(s)
    return Response(content=body,
                    media_type="text/plain; version=0.0.4; charset=utf-8")


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
            reason=body.reason, agent=body.agent, claim_nonce=body.claim_nonce,
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
def poll_request(
    request_id: str,
    x_claim_nonce: str = Header(
        description="The same secret you sent as `claim_nonce` when you "
                    "asked. Proves this poll belongs to the caller that "
                    "raised the request — without it, whoever polled "
                    "fastest after the human clicked Grant would take "
                    "the token."),
):
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
        if not nonce_matches(req, x_claim_nonce):
            # Same answer as an unknown id: a wrong nonce must not
            # confirm that the request exists.
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
    x_sentinel_token: str = Header(
        description="The capability token, exactly as claimed. A HEADER, "
                    "not a query parameter: uvicorn's access log records "
                    "full query strings, so a token in the URL would sit "
                    "in journald in plaintext long after the grant died."),
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
        allowed, reason, grant = check_capability(
            s, token=x_sentinel_token, tool=tool, flow_id=flow_id)
        if not allowed:
            return JSONResponse(status_code=403,
                                content={"allowed": False, "reason": reason})
        return CheckAllowed(
            allowed=True, grant_id=grant.id, flow_id=grant.flow_id,
            tool=grant.tool, expires_at=grant.expires_at,
        )


_FLOW_ID_RE = re.compile(FLOW_ID_PATTERN)


def _authz_deny(reason: str, *, flow_id: str | None, tool: str | None,
                path: str) -> JSONResponse:
    """Audit the refusal, then say no. Pre-check denials (bad headers,
    unparseable body) never reach check_capability, so they write their
    own DENIAL event here — a garbage request still leaves a trail."""
    with SessionLocal() as s:
        audit(s, AuditEventType.DENIAL, flow_id=flow_id, tool=tool,
              details={"source": "ext-authz", "reason": reason, "path": path})
        s.commit()
    return JSONResponse(status_code=403, content={"allowed": False, "reason": reason})


@app.api_route(
    "/v1/ext-authz{original_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
    response_model=CheckAllowed,
    tags=["capability"],
    summary="Envoy ext_authz entry point (the proxy's ONLY question)",
    responses={403: {"model": CheckDenied,
                     "description": "Denied — reason in body, and audited."}},
)
async def ext_authz(original_path: str, request: Request, response: Response):
    """The Sentinel proxy's `SecurityPolicy` points Envoy's ext_authz
    filter here with `path: /v1/ext-authz` (a PREFIX — the original
    request path is appended: `/echo/mcp` arrives as
    `/v1/ext-authz/echo/mcp`) and `bodyToExtAuth` (the original body
    rides along). Identity comes from two forwarded headers,
    `X-Sentinel-Token` and `X-Flow-Id`; the TOOL is never taken from
    the caller — it is derived here from the path + JSON-RPC body
    (see `app.scope`), so a caller cannot name one tool and invoke
    another. Verdict is HTTP status: 200 forwards the request upstream
    (with `X-Sentinel-Grant-Id`/`X-Sentinel-Tool` attached for the MCP
    server's own log), 403 stops it at the proxy. Every deny, including
    unparseable garbage, is audited with its reason."""
    token = request.headers.get("x-sentinel-token")
    flow_id = request.headers.get("x-flow-id")
    body = await request.body()
    if not token:
        return _authz_deny("missing-token", flow_id=flow_id, tool=None,
                           path=original_path)
    if not flow_id:
        return _authz_deny("missing-flow-id", flow_id=None, tool=None,
                           path=original_path)
    if not _FLOW_ID_RE.fullmatch(flow_id):
        return _authz_deny("invalid-flow-id", flow_id=None, tool=None,
                           path=original_path)

    tool, reason = derive_scope(request.method, original_path, body)
    if tool is None:
        return _authz_deny(reason, flow_id=flow_id, tool=None, path=original_path)

    with SessionLocal() as s:
        allowed, reason, grant = check_capability(s, token=token, tool=tool,
                                                  flow_id=flow_id)
        if not allowed:
            return JSONResponse(status_code=403,
                                content={"allowed": False, "reason": reason})
        response.headers["x-sentinel-grant-id"] = grant.id
        response.headers["x-sentinel-tool"] = grant.tool
        return CheckAllowed(
            allowed=True, grant_id=grant.id, flow_id=grant.flow_id,
            tool=grant.tool, expires_at=grant.expires_at,
        )
