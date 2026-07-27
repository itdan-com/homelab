"""Sentinel Admin — the HUMAN-FACING listener (GUI backend + curl).

TRUST-DOMAIN RULE (CLAUDE.md "Trust-domain separation is
non-negotiable"): this app binds to 127.0.0.1 ONLY. Cluster pods reach
the WSL2 host via the k3d network gateway address, which can never
address the host's loopback — so everything here (grant, deny, kill,
audit) is unreachable from inside k3d by construction, before any
auth exists. Auth for the human (WebAuthn/TOTP) lands at 5.5.6; until
then loopback-reachability IS the auth boundary, and the *_by fields
are honesty, not security. The cluster-facing surface lives in
app.broker — never mount granting routes there.

Interactive docs for humans: http://127.0.0.1:8400/docs
"""

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import select, text

from . import __version__
from .db import SessionLocal, engine
from .models import AuditEvent, CapabilityRequest, Flow, RequestStatus
from .schemas import (
    AuditEventOut,
    DenyIn,
    FlowOut,
    GrantIn,
    GrantOut,
    KillIn,
    KillStatus,
    PendingRequest,
    ReleaseIn,
)
from .service import (
    deny_request,
    engage_kill,
    grant_request,
    kill_state,
    refresh_status,
    release_kill,
)

app = FastAPI(
    title="Sentinel Admin (loopback-only)",
    version=__version__,
    description="The human's side of the broker: see what's asking, "
                "grant or deny, pull the kill switch, read the record.",
)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
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
)
def list_pending():
    """The GUI's main panel (and the curl equivalent). Only pending
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
    responses={409: {"description": "Request no longer pending, or kill engaged."}},
)
def grant(request_id: str, body: GrantIn):
    """Mints the token and hands it to the REQUESTER via its poll —
    deliberately never echoed here: the human approves power, they
    don't hold it. 409 if the request already resolved or the kill
    switch is engaged (no new grants while killed)."""
    with SessionLocal() as s:
        req = _get_request(s, request_id)
        try:
            g = grant_request(s, req, ttl_minutes=body.ttl_minutes,
                              granted_by=body.granted_by)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return GrantOut(request_id=req.id, status=req.status.value,
                        grant_id=g.id, expires_at=g.expires_at)


@app.post(
    "/v1/capability-requests/{request_id}/deny",
    tags=["decisions"],
    summary="Say no — resolves the requester's poll immediately",
    responses={409: {"description": "Request no longer pending."}},
)
def deny(request_id: str, body: DenyIn):
    """Fail closed AND loud: the requester's next poll returns `denied`
    with your reason, instead of hanging until timeout."""
    with SessionLocal() as s:
        req = _get_request(s, request_id)
        try:
            deny_request(s, req, denied_by=body.denied_by, reason=body.reason)
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
)
def kill(body: KillIn):
    """Kill is revocation, not pause: every live grant is permanently
    revoked (audited one by one) and /capability-check answers 403
    kill-engaged until released. The state persists in the DB, so a
    Sentinel restart while killed comes back killed. Idempotent."""
    with SessionLocal() as s:
        ks, revoked = engage_kill(s, by=body.engaged_by, reason=body.reason)
        out = KillStatus.model_validate(ks)
        out.grants_revoked = revoked
        return out


@app.post(
    "/v1/kill/release",
    response_model=KillStatus,
    response_model_exclude_none=True,
    tags=["kill"],
    summary="RELEASE: new requests flow again (old tokens stay dead)",
)
def release(body: ReleaseIn):
    """Releasing does NOT resurrect revoked grants — flows re-request
    and the human re-grants. Resumption is a fresh decision, on purpose."""
    with SessionLocal() as s:
        return KillStatus.model_validate(release_kill(s, by=body.released_by))


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


@app.get("/v1/flows", response_model=list[FlowOut], tags=["record"],
         summary="Known flows (active = no ended_at yet)")
def flows(active: bool = Query(default=False)):
    with SessionLocal() as s:
        q = select(Flow).order_by(Flow.started_at.desc())
        if active:
            q = q.where(Flow.ended_at.is_(None))
        return [FlowOut.model_validate(f) for f in s.scalars(q).all()]
