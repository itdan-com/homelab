"""Broker logic — every state transition in one place, both listeners
call into here. Rules of the module:

- Every transition writes its audit event IN THE SAME COMMIT as the
  change it records: no state without a paper trail, ever.
- The kill switch wins every race: it is checked first on the hot
  path and blocks new grants while engaged.
- Kill is REVOCATION, not pause: engaging it permanently revokes all
  live grants. Release means new requests flow again — old tokens
  never come back.
- Grant validity = revoked_at IS NULL ∧ utcnow() < expires_at ∧ kill
  switch off ∧ (tool, flow_id) match exactly.
"""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DEFAULT_GRANT_TTL_MINUTES, REQUEST_TTL_MINUTES, TOKEN_PREFIX
from .models import (
    AuditEvent,
    AuditEventType,
    CapabilityGrant,
    CapabilityRequest,
    Flow,
    KillState,
    RequestStatus,
    utcnow,
)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def audit(
    s: Session,
    event_type: AuditEventType,
    *,
    flow_id: str | None = None,
    tool: str | None = None,
    actor: str | None = None,
    details: dict | None = None,
) -> None:
    s.add(AuditEvent(event_type=event_type, flow_id=flow_id, tool=tool,
                     actor=actor, details=details))


# --- kill switch --------------------------------------------------------------

def kill_state(s: Session) -> KillState:
    ks = s.get(KillState, 1)
    if ks is None:
        ks = KillState(id=1, engaged=False)
        s.add(ks)
        s.commit()
    return ks


def engage_kill(s: Session, by: str, reason: str | None) -> tuple[KillState, int]:
    """Engage + permanently revoke every live grant. Idempotent."""
    ks = kill_state(s)
    now = utcnow()
    revoked = 0
    if not ks.engaged:
        ks.engaged, ks.engaged_at, ks.engaged_by, ks.reason = True, now, by, reason
        ks.released_at = ks.released_by = None
        live = s.scalars(
            select(CapabilityGrant).where(
                CapabilityGrant.revoked_at.is_(None),
                CapabilityGrant.expires_at > now,
            )
        ).all()
        for g in live:
            g.revoked_at = now
            revoked += 1
            audit(s, AuditEventType.REVOCATION, flow_id=g.flow_id, tool=g.tool,
                  actor=by, details={"cause": "global-kill", "grant_id": g.id})
        audit(s, AuditEventType.KILL_ENGAGED, actor=by,
              details={"reason": reason, "grants_revoked": revoked})
        s.commit()
    return ks, revoked


def release_kill(s: Session, by: str) -> KillState:
    """Release: new requests/grants flow again. Revoked grants stay dead."""
    ks = kill_state(s)
    if ks.engaged:
        ks.engaged, ks.released_at, ks.released_by = False, utcnow(), by
        audit(s, AuditEventType.KILL_RELEASED, actor=by)
        s.commit()
    return ks


# --- requests -----------------------------------------------------------------

def create_request(
    s: Session, flow_id: str, tool: str, reason: str, agent: str
) -> tuple[CapabilityRequest, bool]:
    """Register the flow if unseen; dedupe onto an existing pending
    request for the same (flow, tool) so client retries don't spam the
    GUI. Returns (request, created)."""
    if s.get(Flow, flow_id) is None:
        s.add(Flow(id=flow_id, agent=agent))

    existing = s.scalars(
        select(CapabilityRequest).where(
            CapabilityRequest.flow_id == flow_id,
            CapabilityRequest.tool == tool,
            CapabilityRequest.status == RequestStatus.PENDING,
        )
    ).first()
    if existing is not None and refresh_status(s, existing) == RequestStatus.PENDING:
        return existing, False

    req = CapabilityRequest(
        flow_id=flow_id, tool=tool, reason=reason,
        expires_at=utcnow() + timedelta(minutes=REQUEST_TTL_MINUTES),
    )
    s.add(req)
    audit(s, AuditEventType.REQUEST, flow_id=flow_id, tool=tool, actor=agent,
          details={"reason": reason})
    s.commit()
    return req, True


def refresh_status(s: Session, req: CapabilityRequest) -> RequestStatus:
    """Lazy expiry (no background sweeper in the MVP): pending requests
    lapse at their own expires_at; a granted-but-unclaimed token is
    cleared the moment its grant stops being live."""
    changed = False
    if req.status == RequestStatus.PENDING and utcnow() >= req.expires_at:
        req.status = RequestStatus.EXPIRED
        changed = True
    if req.token_plaintext is not None and req.grant is not None:
        if req.grant.revoked_at is not None or utcnow() >= req.grant.expires_at:
            req.token_plaintext = None
            changed = True
    if changed:
        s.commit()
    return req.status


def claim_token(s: Session, req: CapabilityRequest) -> str | None:
    """One-time token pickup by the requester: first granted-status poll
    gets the plaintext, every later poll gets None."""
    tok = req.token_plaintext
    if tok is not None:
        req.token_plaintext = None
        req.token_claimed_at = utcnow()
        s.commit()
    return tok


def grant_request(
    s: Session, req: CapabilityRequest, ttl_minutes: int, granted_by: str
) -> CapabilityGrant:
    """Mint the token (returned to the REQUESTER via poll, never to the
    caller of this function), store only its hash, flip the request.
    Raises ValueError if the request is not pending or kill is engaged."""
    if kill_state(s).engaged:
        raise ValueError("kill switch engaged — no new grants")
    if refresh_status(s, req) != RequestStatus.PENDING:
        raise ValueError(f"request is {req.status}, not pending")

    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    grant = CapabilityGrant(
        flow_id=req.flow_id, tool=req.tool, token_hash=_hash(token),
        expires_at=now + timedelta(minutes=ttl_minutes), granted_by=granted_by,
    )
    s.add(grant)
    s.flush()  # grant.id
    req.status = RequestStatus.GRANTED
    req.decided_at, req.decided_by, req.grant_id = now, granted_by, grant.id
    req.token_plaintext = token
    audit(s, AuditEventType.GRANT, flow_id=req.flow_id, tool=req.tool,
          actor=granted_by, details={"grant_id": grant.id, "ttl_minutes": ttl_minutes})
    s.commit()
    return grant


def deny_request(
    s: Session, req: CapabilityRequest, denied_by: str, reason: str | None
) -> CapabilityRequest:
    if refresh_status(s, req) != RequestStatus.PENDING:
        raise ValueError(f"request is {req.status}, not pending")
    req.status = RequestStatus.DENIED
    req.decided_at, req.decided_by, req.denied_reason = utcnow(), denied_by, reason
    audit(s, AuditEventType.DENIAL, flow_id=req.flow_id, tool=req.tool,
          actor=denied_by, details={"source": "human", "reason": reason})
    s.commit()
    return req


# --- the hot path -------------------------------------------------------------

def check_capability(
    s: Session, token: str, tool: str, flow_id: str
) -> tuple[bool, str, CapabilityGrant | None]:
    """The proxy's question: may this call proceed? Kill first (fail
    closed beats fast), then hash lookup, then exact scope match, then
    liveness. Every answer — allow or deny — is audited."""
    if kill_state(s).engaged:
        audit(s, AuditEventType.DENIAL, flow_id=flow_id, tool=tool,
              details={"source": "check", "reason": "kill-engaged"})
        s.commit()
        return False, "kill-engaged", None

    grant = s.scalars(
        select(CapabilityGrant).where(CapabilityGrant.token_hash == _hash(token))
    ).first()

    reason = None
    if grant is None:
        reason = "unknown-token"
    elif grant.tool != tool or grant.flow_id != flow_id:
        reason = "scope-mismatch"
    elif grant.revoked_at is not None:
        reason = "revoked"
    elif utcnow() >= grant.expires_at:
        reason = "expired"

    if reason is not None:
        audit(s, AuditEventType.DENIAL, flow_id=flow_id, tool=tool,
              details={"source": "check", "reason": reason,
                       "grant_id": grant.id if grant else None})
        s.commit()
        return False, reason, None

    audit(s, AuditEventType.USE, flow_id=flow_id, tool=tool,
          details={"grant_id": grant.id})
    s.commit()
    return True, "ok", grant
