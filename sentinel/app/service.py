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
    """Engage + permanently revoke every live grant. Idempotent.

    The sweep runs on EVERY press, not only the first. Pressing kill a
    second time used to be a guaranteed no-op — so if a grant ever
    became live while engaged (a grant committing concurrently with the
    press, say), the operator's instinct of hitting the switch again
    could not clean it up, and the grant came back alive on release.
    A kill switch that cannot be pressed twice is not a kill switch."""
    ks = kill_state(s)
    now = utcnow()
    first_press = not ks.engaged
    if first_press:
        ks.engaged, ks.engaged_at, ks.engaged_by, ks.reason = True, now, by, reason
        ks.released_at = ks.released_by = None

    live = s.scalars(
        select(CapabilityGrant).where(
            CapabilityGrant.revoked_at.is_(None),
            CapabilityGrant.expires_at > now,
        )
    ).all()
    revoked = 0
    for g in live:
        g.revoked_at = now
        revoked += 1
        audit(s, AuditEventType.REVOCATION, flow_id=g.flow_id, tool=g.tool,
              actor=by, details={"cause": "global-kill", "grant_id": g.id})
    if first_press or revoked:
        audit(s, AuditEventType.KILL_ENGAGED, actor=by,
              details={"reason": reason, "grants_revoked": revoked,
                       "repeat_press": not first_press})
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
    s: Session, flow_id: str, tool: str, reason: str, agent: str, claim_nonce: str
) -> tuple[CapabilityRequest, bool]:
    """Register the flow if unseen; dedupe onto an existing pending
    request so client retries don't spam the console. Returns
    (request, created).

    Dedupe matches on the CLAIM NONCE as well as (flow, tool) — and
    that is a security property, not tidiness. Matching on scope alone
    meant any caller could post a request naming someone else's flow
    and tool, be handed back *their* request_id, and then race them to
    the one-time token pickup. Same-nonce means same caller retrying;
    a different nonce is a different asker and gets its own card in
    front of the human, with its own justification attached."""
    if s.get(Flow, flow_id) is None:
        s.add(Flow(id=flow_id, agent=agent))

    nonce_hash = _hash(claim_nonce)
    existing = s.scalars(
        select(CapabilityRequest).where(
            CapabilityRequest.flow_id == flow_id,
            CapabilityRequest.tool == tool,
            CapabilityRequest.claim_nonce_hash == nonce_hash,
            CapabilityRequest.status == RequestStatus.PENDING,
        )
    ).first()
    if existing is not None and refresh_status(s, existing) == RequestStatus.PENDING:
        return existing, False

    req = CapabilityRequest(
        flow_id=flow_id, tool=tool, reason=reason, claim_nonce_hash=nonce_hash,
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


def nonce_matches(req: CapabilityRequest, claim_nonce: str | None) -> bool:
    """Is this poller the caller that raised the request? Rows created
    before 5.5.5 carry no nonce; they are long resolved, and refusing
    them outright would be a lie about why."""
    if req.claim_nonce_hash is None:
        return True
    return claim_nonce is not None and _hash(claim_nonce) == req.claim_nonce_hash


def claim_token(s: Session, req: CapabilityRequest) -> str | None:
    """One-time token pickup by the requester: the first granted-status
    poll FROM THE CALLER THAT ASKED (see nonce_matches) gets the
    plaintext, every later poll gets None."""
    tok = req.token_plaintext
    if tok is not None:
        req.token_plaintext = None
        req.token_claimed_at = utcnow()
        audit(s, AuditEventType.CLAIM, flow_id=req.flow_id, tool=req.tool,
              details={"grant_id": req.grant_id})
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
    # Re-read the switch after the checks above: a kill engaged while
    # this grant was being assembled must win the race. SQLite
    # serializes writers, so by the time we are here the sweep either
    # ran (and we abort) or has not started (and it will see this row).
    if kill_state(s).engaged:
        raise ValueError("kill switch engaged — no new grants")
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
