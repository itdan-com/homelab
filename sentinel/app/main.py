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
every state-changing route, none of which is authentication:

  1. Host allowlist   — defeats DNS rebinding (`Host: evil.com` → 400)
  2. Origin check     — defeats plain cross-site requests
  3. Console header   — a custom header forces a CORS preflight that
                        this app never answers, so the browser refuses
                        before Sentinel is asked

**And, since 5.5.6, authentication on top of all three**: every route
that reads or changes platform state requires a session opened by a
WebAuthn passkey (`app.auth`). Reads are gated too — the pending panel
and the audit log describe what the platform's agent is trying to do
and what it has done, which is not public merely because it is not a
button. The actor is resolved server-side from that session, so the
audit log records a cryptographically established human rather than a
name the caller typed.

Console: http://localhost:8400/  — the hostname matters. WebAuthn's
Relying Party ID must be a domain, so `localhost` works and
`127.0.0.1` does not.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from . import auth_routes
from . import policy
from .actor import console_guard, current_operator, require_operator
from .config import (
    CONSOLE_ALLOWED_HOSTS,
    CONSOLE_ORIGIN,
    FLOW_ACTIVE_MINUTES,
    POLICY_RELOAD_SECONDS,
)
from .db import SessionLocal, engine
from .models import (
    AuditEvent,
    AuditEventType,
    CapabilityGrant,
    CapabilityRequest,
    Flow,
    RequestStatus,
    utcnow,
)
from .policy import PolicyError
from .schemas import (
    AuditEventOut,
    DenyIn,
    FlowOut,
    FlowRevokeOut,
    GrantIn,
    GrantOut,
    GrantRevokeOut,
    GrantRow,
    KillIn,
    KillStatus,
    PendingRequest,
    PolicyActivateOut,
    PolicyHistoryRow,
    PolicyRevertIn,
    PolicyStatusOut,
    PolicyStoreIn,
    PolicyStoreOut,
    PolicyStructuredIn,
    RevokeIn,
)
from .service import (
    audit,
    mint_forwarding_token,
    deny_request,
    engage_kill,
    grant_request,
    kill_state,
    refresh_status,
    release_kill,
    revoke_flow,
    revoke_grant,
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

def _refuse_unsafe_exposure() -> None:
    """Refuse to start exposed without TLS.

    A `--host 0.0.0.0` typo in a unit file would put the kill switch on
    the network in cleartext, and nothing else in the system would
    notice: the app would look healthy, the console would work, and the
    session cookie would cross the wire in the clear. WebAuthn does not
    save us — it needs a secure context, so the passkey would simply
    stop working while the surface stayed open.

    So the process refuses. In cloud (ADR-004) the console IS
    network-reachable; the rule is not "loopback forever", it is "not
    exposed without https".
    """
    bind = os.environ.get("SENTINEL_ADMIN_BIND", "127.0.0.1")
    loopback = bind in {"127.0.0.1", "::1", "localhost"} or bind.startswith("127.")
    if not loopback and not CONSOLE_ORIGIN.startswith("https://"):
        raise RuntimeError(
            f"refusing to start: admin console bound to {bind} (not loopback) "
            f"while SENTINEL_CONSOLE_ORIGIN is {CONSOLE_ORIGIN!r}. Serve it "
            "over https and set that origin, or bind loopback."
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup checks. Deliberately a lifespan handler and not the older
    @app.on_event: that API is deprecated, and a guard that quietly
    stops running when a framework drops a hook is a security control
    with an expiry date nobody notices."""
    _refuse_unsafe_exposure()
    # Best-effort policy activation (7.2.2). Failure must NOT stop the
    # console — the console is where a broken store gets fixed — and
    # an inactive store fails the person-path closed, never open.
    # POLICY_DIR is passed explicitly so it is read at call time, not
    # frozen as a default argument at import (7.3.1).
    try:
        policy.activate(policy.POLICY_DIR, actor="startup")
    except Exception as e:  # PolicyError, missing store, no git, …
        import logging
        logging.getLogger("sentinel").warning("policy store not active: %s", e)
    # 7.3.1: the admin process watches the store too. Console saves
    # already swap this process's active policy directly; the watcher
    # covers every OTHER writer (a root hand-edit, tooling, a future
    # second author) so admin and broker converge on the same bytes no
    # matter whose hand moved them.
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
    title="Sentinel Admin (loopback-only)",
    version=__version__,
    description="The human's side of the broker: see what's asking, "
                "grant or deny, pull the kill switch, read the record.",
    # Swagger UI loads its JavaScript from a public CDN. This origin owns
    # the kill switch, so it executes no third-party code — the CSP above
    # would block it anyway, and a page that renders broken is worse than
    # one that is honestly absent. The schema itself stays at
    # /openapi.json, and app/schemas.py's Field descriptions remain the
    # generated-from-code reference (see sentinel/README.md).
    docs_url=None,
    redoc_url=None,
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


app.include_router(auth_routes.router)
app.mount("/static", StaticFiles(directory=CONSOLE_DIR), name="static")


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(CONSOLE_DIR / "index.html")


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    """Deliberately unauthenticated and deliberately uninformative: a
    health check exists to tell a supervisor the process is alive, so it
    must work before anyone signs in and must not describe who can. Who
    you are signed in as is `/auth/status`."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "listener": "admin", "version": __version__}


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
    dependencies=[Depends(require_operator)],
)
def list_pending():
    """The console's main panel (and the curl equivalent). Only pending
    requests appear; expiry is applied on read."""
    with SessionLocal() as s:
        rows = s.scalars(
            select(CapabilityRequest)
            .where(CapabilityRequest.status == RequestStatus.PENDING)
            .order_by(CapabilityRequest.requested_at)
            # Bounded: nothing rate-limits capability requests yet, and an
            # unbounded approval screen is a denial-of-service on the human
            # AND on the hot path they share a database with.
            .limit(200)
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
         tags=["kill"], summary="Kill-switch state",
         dependencies=[Depends(require_operator)])
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
    dependencies=[Depends(require_operator)],
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
    dependencies=[Depends(require_operator)],
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


@app.get(
    "/v1/upstream-credentials",
    tags=["record"],
    summary="Which MCP servers have a credential, and of what kind",
    dependencies=[Depends(require_operator)],
)
def upstream_credentials():
    """Never returns a secret — only enough to recognise which key is
    installed (App id, key fingerprint) and how long the current
    short-lived token has left."""
    from . import upstream_auth
    from .config import MCP_UPSTREAM_TOKENS_FILE
    try:
        return {"servers": upstream_auth.describe(MCP_UPSTREAM_TOKENS_FILE)}
    except upstream_auth.UpstreamAuthError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put(
    "/v1/upstream-credentials/{server}",
    tags=["decisions"],
    summary="Install or replace an MCP server's upstream credential",
    dependencies=[Depends(console_guard)],
)
def save_upstream_credential(server: str, body: dict,
                             operator: str = Depends(current_operator)):
    """Paste-and-save, so a credential never requires shell access to
    the host. Passkey-gated and audited like every other console write;
    the secret itself is never echoed back and never logged."""
    from . import upstream_auth
    from .config import MCP_UPSTREAM_TOKENS_FILE
    try:
        entry = upstream_auth.save(MCP_UPSTREAM_TOKENS_FILE, server, body)
    except upstream_auth.UpstreamAuthError as e:
        _audit_policy_change(operator, {"action": "upstream-credential-rejected",
                                        "server": server, "error": str(e)})
        raise HTTPException(status_code=422, detail=str(e))
    _audit_policy_change(operator, {
        "action": "upstream-credential-saved", "server": server,
        "kind": "app" if entry.get("app_id") else "token",
        "app_id": entry.get("app_id"),
        "key_fingerprint": entry.get("key_fingerprint")})
    return {"server": server, "saved": True,
            "kind": "app" if entry.get("app_id") else "token"}


@app.delete(
    "/v1/upstream-credentials/{server}",
    tags=["decisions"],
    summary="Remove an MCP server's upstream credential",
    dependencies=[Depends(console_guard)],
)
def delete_upstream_credential(server: str,
                               operator: str = Depends(current_operator)):
    from . import upstream_auth
    from .config import MCP_UPSTREAM_TOKENS_FILE
    removed = upstream_auth.remove(MCP_UPSTREAM_TOKENS_FILE, server)
    if not removed:
        raise HTTPException(status_code=404, detail="no credential for that server")
    _audit_policy_change(operator, {"action": "upstream-credential-removed",
                                    "server": server})
    return {"server": server, "removed": True}


@app.post(
    "/v1/upstream-credentials/{server}/discover",
    tags=["decisions"],
    summary="Ask a connected server what it can do, and classify it",
    dependencies=[Depends(console_guard)],
)
def discover_server_tools(server: str, operator: str = Depends(current_operator)):
    """Registering a server should not be followed by a human retyping
    its verbs into YAML. MCP tools declare `readOnlyHint` and
    `destructiveHint`, so the server describes itself and the platform
    writes the classification.

    Discovery PROPOSES and never widens: destructive verbs come back
    listed but unclassified, and an unclassified tool is denied — so a
    human still decides before anything dangerous becomes callable."""
    from . import upstream_auth
    from .config import (MCP_PROXY_BASE, MCP_UPSTREAM_TOKENS_FILE,
                         OIDC_CA_BUNDLE)
    url = (upstream_auth.upstream_url(server, MCP_UPSTREAM_TOKENS_FILE)
           or f"{MCP_PROXY_BASE}/{server}/mcp")
    import secrets as _secrets
    flow_id = f"discover-{_secrets.token_urlsafe(6)}"
    with SessionLocal() as s:
        gate = mint_forwarding_token(s, flow_id=flow_id, principal=None,
                                     tool=f"{server}.rpc.tools.list",
                                     ttl_seconds=60)
    try:
        token = upstream_auth.token_for(server, MCP_UPSTREAM_TOKENS_FILE)
        found = upstream_auth.discover_tools(
            server, url, token, OIDC_CA_BUNDLE,
            gate_headers={"X-Sentinel-Token": gate, "X-Flow-Id": flow_id})
    except upstream_auth.UpstreamAuthError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{server}: {e}")

    docs = policy.store_documents(policy.POLICY_DIR)
    import yaml as _yaml
    servers = _yaml.safe_load(docs.get("servers") or "") or {}
    entry = servers.setdefault(server, {})
    previous = entry.get("tools") or {}
    entry["tools"] = {"read": found["read"], "write": found["write"]}
    docs["servers"] = _yaml.safe_dump(servers, sort_keys=False)
    try:
        ap = policy.save_and_activate(policy.POLICY_DIR, docs, actor=operator)
    except PolicyError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    _audit_policy_change(operator, {
        "action": "server-tools-discovered", "server": server,
        "read": len(found["read"]), "write": len(found["write"]),
        "left_unclassified": found["destructive"],
        "previous": previous}, version=ap.version)
    return {"server": server, "policy_version": ap.version, **found}


@app.get(
    "/v1/policy/status",
    response_model=PolicyStatusOut,
    response_model_exclude_none=True,
    tags=["record"],
    summary="Which policy version is deciding right now",
    dependencies=[Depends(require_operator)],
)
def policy_status():
    """`active: false` means no store has activated (missing, or the
    last edit failed validation and last-good was NONE) — the person
    path denies closed in that state, and the Access screen (7.2.4)
    is where it gets fixed."""
    ap = policy.get_active()
    if ap is None:
        return PolicyStatusOut(active=False)
    return PolicyStatusOut(
        active=True, version=ap.version, loaded_at=ap.loaded_at,
        servers=sorted(ap.servers),
        matrix_groups=sorted(ap.matrix.get("grants") or {}),
    )


# --- the policy store: the Access screen's API (7.2.4, ADR-005 D5) ------------

def _audit_policy_change(operator: str, details: dict,
                         version: str | None = None) -> None:
    with SessionLocal() as s:
        audit(s, AuditEventType.POLICY_CHANGE, actor=operator,
              policy_version=version, details=details)
        s.commit()


@app.get(
    "/v1/policy/store",
    response_model=PolicyStoreOut,
    response_model_exclude_none=True,
    tags=["access"],
    summary="The store: editor texts + the parsed view the grid renders",
    dependencies=[Depends(require_operator)],
)
def policy_store():
    """Documents come from DISK (always last-good-or-better); the
    parsed fields come from the ACTIVE policy — when nothing is
    active, the editors still work, because editing is how a broken
    store gets fixed."""
    docs = policy.store_documents(policy.POLICY_DIR)
    ap = policy.get_active()
    if ap is None:
        return PolicyStoreOut(active=False, documents=docs)
    return PolicyStoreOut(
        active=True, version=ap.version, loaded_at=ap.loaded_at,
        documents=docs, groups=ap.groups, people=ap.people,
        matrix=ap.matrix, servers=sorted(ap.servers),
        servers_detail=ap.servers,
    )


@app.put(
    "/v1/policy/store",
    response_model=PolicyActivateOut,
    response_model_exclude_none=True,
    tags=["access"],
    summary="Save & activate — validate first, reject without touching disk",
    dependencies=[Depends(console_guard)],
    responses={422: {"description": "Rejected — every error listed; the "
                                    "store on disk is untouched and "
                                    "last-good keeps serving."}},
)
def policy_save(body: PolicyStoreIn, operator: str = Depends(current_operator)):
    prev = policy.get_active().version if policy.get_active() else None
    try:
        ap = policy.save_and_activate(policy.POLICY_DIR, body.model_dump(),
                                      actor=operator)
    except PolicyError as e:
        # Rejections are audited on purpose: a stream of them is
        # somebody probing the policy surface.
        _audit_policy_change(operator, {"result": "rejected",
                                        "errors": e.errors[:20]})
        raise HTTPException(status_code=422, detail=e.errors)
    _audit_policy_change(operator, {"result": "activated",
                                    "version": ap.version,
                                    "previous_version": prev}, ap.version)
    return PolicyActivateOut(version=ap.version, previous_version=prev)


@app.put(
    "/v1/policy/store/structured",
    response_model=PolicyActivateOut,
    response_model_exclude_none=True,
    tags=["access"],
    summary="The GUI's save — same gate, structured input",
    dependencies=[Depends(console_guard)],
    responses={422: {"description": "Rejected — every error listed; "
                                    "disk untouched, last-good serving."}},
)
def policy_save_structured(body: PolicyStructuredIn,
                           operator: str = Depends(current_operator)):
    """Serializes the edited objects to the store's YAML documents and
    rides the exact validate→activate path a raw save rides. The
    overlay is preserved from disk unless explicitly supplied — the
    GUI's save must never silently blank the escape hatch."""
    docs = policy.structured_to_documents(body.groups, body.people,
                                          body.matrix, body.servers)
    docs["overlay"] = (body.overlay if body.overlay is not None
                       else policy.store_documents(policy.POLICY_DIR)["overlay"])
    prev = policy.get_active().version if policy.get_active() else None
    try:
        ap = policy.save_and_activate(policy.POLICY_DIR, docs, actor=operator)
    except PolicyError as e:
        _audit_policy_change(operator, {"result": "rejected", "via": "gui",
                                        "errors": e.errors[:20]})
        raise HTTPException(status_code=422, detail=e.errors)
    _audit_policy_change(operator, {"result": "activated", "via": "gui",
                                    "version": ap.version,
                                    "previous_version": prev}, ap.version)
    return PolicyActivateOut(version=ap.version, previous_version=prev)


@app.get(
    "/v1/policy/history",
    response_model=list[PolicyHistoryRow],
    tags=["access"],
    summary="Activated versions, newest first (the store's own git)",
    dependencies=[Depends(require_operator)],
)
def policy_history():
    ap = policy.get_active()
    current = ap.version if ap else None
    return [PolicyHistoryRow(version=r["version"], actor=r["actor"],
                             ts=r["ts"], current=r["version"] == current)
            for r in policy.history(policy.POLICY_DIR)]


@app.post(
    "/v1/policy/revert",
    response_model=PolicyActivateOut,
    response_model_exclude_none=True,
    tags=["access"],
    summary="Restore version N — forward, nothing rewritten",
    dependencies=[Depends(console_guard)],
    responses={422: {"description": "Unknown version, or the restored "
                                    "store no longer validates."}},
)
def policy_revert(body: PolicyRevertIn,
                  operator: str = Depends(current_operator)):
    prev = policy.get_active().version if policy.get_active() else None
    try:
        ap = policy.revert_to(policy.POLICY_DIR, body.version, actor=operator)
    except PolicyError as e:
        _audit_policy_change(operator, {"result": "rejected",
                                        "revert_to": body.version,
                                        "errors": e.errors[:20]})
        raise HTTPException(status_code=422, detail=e.errors)
    _audit_policy_change(operator, {"result": "activated",
                                    "version": ap.version,
                                    "previous_version": prev,
                                    "revert_to": body.version}, ap.version)
    return PolicyActivateOut(version=ap.version, previous_version=prev)


# --- grants & revocation (7.2.1, ADR-004 debt 4) ------------------------------

@app.get(
    "/v1/grants",
    response_model=list[GrantRow],
    response_model_exclude_none=True,
    tags=["record"],
    summary="Grants, newest first — the revocation surface reads this",
    dependencies=[Depends(require_operator)],
)
def grants(
    live: bool = Query(default=False,
                       description="Only grants neither revoked nor expired."),
    flow_id: str | None = Query(default=None, description="Filter to one flow."),
    limit: int = Query(default=50, ge=1, le=500),
):
    now = utcnow()
    with SessionLocal() as s:
        q = select(CapabilityGrant).order_by(
            CapabilityGrant.granted_at.desc()).limit(limit)
        if flow_id:
            q = q.where(CapabilityGrant.flow_id == flow_id)
        if live:
            q = q.where(CapabilityGrant.revoked_at.is_(None),
                        CapabilityGrant.expires_at > now)
        return [
            GrantRow(
                grant_id=g.id, flow_id=g.flow_id,
                principal=g.principal.email if g.principal_id else None,
                tool=g.tool, profile=g.profile, tools=g.tools_json,
                granted_at=g.granted_at, expires_at=g.expires_at,
                granted_by=g.granted_by, granted_via=g.granted_via,
                revoked_at=g.revoked_at,
                live=g.revoked_at is None and g.expires_at > now,
            )
            for g in s.scalars(q).all()
        ]


@app.post(
    "/v1/grants/{grant_id}/revoke",
    response_model=GrantRevokeOut,
    tags=["decisions"],
    summary="Revoke ONE grant — the middle ground the kill switch never had",
    dependencies=[Depends(console_guard)],
    responses={409: {"description": "Grant already revoked or expired."}},
)
def revoke_one(grant_id: str, body: RevokeIn,
               operator: str = Depends(current_operator)):
    """ADR-004 debt 4: "stop that one thing" no longer requires nuking
    every flow on the platform. The token's next check answers 403
    `revoked`; nothing else is touched."""
    with SessionLocal() as s:
        g = s.get(CapabilityGrant, grant_id)
        if g is None:
            raise HTTPException(status_code=404, detail="unknown grant_id")
        try:
            revoke_grant(s, g, by=operator, reason=body.reason)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return GrantRevokeOut(grant_id=g.id, revoked_at=g.revoked_at)


@app.post(
    "/v1/flows/{flow_id}/revoke",
    response_model=FlowRevokeOut,
    tags=["decisions"],
    summary="Revoke every live grant of one flow",
    dependencies=[Depends(console_guard)],
)
def revoke_whole_flow(flow_id: str, body: RevokeIn,
                      operator: str = Depends(current_operator)):
    """Zero revoked is a success ("this flow now provably holds
    nothing"); 404 only for a flow Sentinel has never seen at all."""
    with SessionLocal() as s:
        if s.get(Flow, flow_id) is None:
            raise HTTPException(status_code=404, detail="unknown flow_id")
        n = revoke_flow(s, flow_id, by=operator, reason=body.reason)
        return FlowRevokeOut(flow_id=flow_id, grants_revoked=n)
