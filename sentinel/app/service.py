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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import (
    DEFAULT_GRANT_TTL_MINUTES,
    REQUEST_TTL_MINUTES,
    TOKEN_PREFIX,
    VELOCITY_WINDOWS_MINUTES,
)
from .models import (
    AuditEvent,
    AuditEventType,
    CapabilityGrant,
    CapabilityRequest,
    Flow,
    IdpMigration,
    KillState,
    Principal,
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
    principal: str | None = None,
    resource: str | None = None,
    tier: str | None = None,
    policy_version: str | None = None,
) -> None:
    s.add(AuditEvent(event_type=event_type, flow_id=flow_id, tool=tool,
                     actor=actor, details=details, principal=principal,
                     resource=resource, tier=tier, policy_version=policy_version))


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
    # Airlock's APPROVE door (7.3.5): a request a PERSON raised for a
    # PROFILE is granted as a profile grant hung on that person, not as
    # a flow-scoped single-tool token. Same console card, same button,
    # same human — `granted_via` records which door it came through,
    # and `approve` is the only value a ladder `approve` rung accepts.
    if req.profile and req.principal_id:
        from . import policy
        server, _, level = req.profile.partition(":")
        ap = policy.get_active()
        tools = policy.profile_tools(ap.servers, server, level) if ap else []
        if not tools:
            raise ValueError(f"profile {req.profile!r} covers no tools in the "
                             "active policy — refusing to grant nothing")
        grant, _plain = mint_profile_grant(
            s, profile=req.profile, tools=tools, window_minutes=ttl_minutes,
            granted_by=granted_by, granted_via="approve",
            principal=s.get(Principal, req.principal_id), flow_id=req.flow_id)
        req.status = RequestStatus.GRANTED
        req.decided_at, req.decided_by, req.grant_id = now, granted_by, grant.id
        # No token_plaintext: nobody polls for this one. The person's
        # authority is the grant itself, checked per call at the door.
        s.commit()
        return grant
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

def _grant_covers(grant: CapabilityGrant, tool: str) -> bool:
    """Single-tool grants match exactly; profile grants (7.2.1) match
    membership in the tool-set SNAPSHOT taken at mint time. The live
    policy store is deliberately not consulted here — a profile edit
    after mint must never widen a grant already in the wild.

    Snapshot entries ending `.*` are prefix classes (7.2.2's tool
    classification writes e.g. `github.rpc.*` into read profiles, so
    the MCP handshake rides one grant): `github.rpc.*` covers
    `github.rpc.tools.list` and the bare `github.rpc`, and covers
    nothing that merely shares the letters (`github.rpcx` is not a
    match — the dot is part of the prefix)."""
    if grant.tools_json:
        for entry in grant.tools_json:
            if entry.endswith(".*"):
                if tool.startswith(entry[:-1]) or tool == entry[:-2]:
                    return True
            elif tool == entry:
                return True
        return False
    return grant.tool == tool


def actions_in_window(
    s: Session, principal_email: str, tool: str, tier: str,
) -> dict[str, int]:
    """The velocity signal (ADR-007 Decision 1): how many times this
    principal has SUCCESSFULLY used this exact (tool, tier) inside each
    of app.config.VELOCITY_WINDOWS_MINUTES's trailing windows, computed
    fresh on every call — no cache, anywhere, so a rule referencing this
    can never see a stale count and a grant offer can never outlive the
    moment that made it available.

    Counts AuditEventType.USE rows only: a denied attempt did not
    complete, so only completed calls count toward "stop action N" —
    which is the entire point of a velocity rule. Keyed on (principal,
    tool, tier), deliberately NOT the concrete resource: "wipe laptop A"
    and "wipe laptop B" must accumulate toward the SAME count, or a
    bulk action spread across many resources could never be caught at
    all — the scenario this decision exists for.

    One indexed COUNT per window rather than one clever combined query:
    there are only ever two or three windows, and a query per window
    stays trivially auditable against ix_audit_events_velocity — the
    same trade this codebase already makes elsewhere in favor of
    readable over clever."""
    now = utcnow()
    out: dict[str, int] = {}
    for key, minutes in VELOCITY_WINDOWS_MINUTES.items():
        since = now - timedelta(minutes=minutes)
        out[key] = s.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.USE,
                AuditEvent.principal == principal_email,
                AuditEvent.tool == tool,
                AuditEvent.tier == tier,
                AuditEvent.ts >= since,
            )
        ) or 0
    return out


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
    elif grant.flow_id is None:
        # Principal-bound profile grants carry no flow binding; their
        # door — the 7.3 gateway path that authenticates the PERSON —
        # does not exist yet. On this flow-header path they are out of
        # scope by definition: deny closed, never "not yet checked".
        reason = "scope-mismatch"
    elif grant.flow_id != flow_id or not _grant_covers(grant, tool):
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
          principal=grant.principal.email if grant.principal_id else None,
          details={"grant_id": grant.id,
                   **({"profile": grant.profile} if grant.profile else {})})
    s.commit()
    return True, "ok", grant


# --- revocation & profiles (7.2.1, ADR-005) -----------------------------------

def revoke_grant(
    s: Session, grant: CapabilityGrant, by: str, reason: str | None = None,
    cause: str = "manual",
) -> CapabilityGrant:
    """Per-grant revoke — ADR-004 debt 4's middle ground: between "wait
    for expiry" and "kill everything" there was nothing, which made the
    kill switch the only tool and therefore one operators hesitate to
    use. Revoking a dead grant is a ValueError, not a no-op — an
    operator reaching for this button deserves to know it did nothing."""
    now = utcnow()
    if grant.revoked_at is not None:
        raise ValueError("grant already revoked")
    if now >= grant.expires_at:
        raise ValueError("grant already expired")
    grant.revoked_at = now
    audit(s, AuditEventType.REVOCATION, flow_id=grant.flow_id, tool=grant.tool,
          actor=by,
          principal=grant.principal.email if grant.principal_id else None,
          details={"cause": cause, "grant_id": grant.id, "reason": reason,
                   **({"profile": grant.profile} if grant.profile else {})})
    s.commit()
    return grant


def revoke_flow(s: Session, flow_id: str, by: str, reason: str | None = None) -> int:
    """Revoke every live grant of one flow (ADR-004 debt 4). Returns
    the count; zero is a valid answer, not an error — "make sure this
    flow holds nothing" is a legitimate wish for a flow that already
    holds nothing."""
    now = utcnow()
    live = s.scalars(
        select(CapabilityGrant).where(
            CapabilityGrant.flow_id == flow_id,
            CapabilityGrant.revoked_at.is_(None),
            CapabilityGrant.expires_at > now,
        )
    ).all()
    for g in live:
        g.revoked_at = now
        audit(s, AuditEventType.REVOCATION, flow_id=flow_id, tool=g.tool,
              actor=by, details={"cause": "flow-revoke", "grant_id": g.id,
                                 "reason": reason})
    s.commit()
    return len(live)


def mint_profile_grant(
    s: Session, *, profile: str, tools: list[str], window_minutes: int,
    granted_by: str, granted_via: str, principal: Principal | None = None,
    flow_id: str | None = None,
) -> tuple[CapabilityGrant, str]:
    """The elevation primitive (ADR-005 Decision 6): a SET of tools for
    a WINDOW, hung on a person. `tools` is SNAPSHOTTED into the grant —
    a policy-store edit after mint changes nothing for grants already
    in the wild.

    Returns (grant, plaintext token). The confirm/approve doors (7.3)
    deliver the token over the caller's own authenticated channel, so
    there is no claim-once poll dance on this path; the API layer that
    calls this must never log the token."""
    if kill_state(s).engaged:
        raise ValueError("kill switch engaged — no new grants")
    if not tools:
        raise ValueError("profile grant needs a non-empty tool set")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    grant = CapabilityGrant(
        flow_id=flow_id,
        principal_id=principal.id if principal else None,
        tool=f"profile:{profile}", profile=profile, tools_json=list(tools),
        granted_via=granted_via, token_hash=_hash(token),
        expires_at=utcnow() + timedelta(minutes=window_minutes),
        granted_by=granted_by,
    )
    s.add(grant)
    s.flush()
    audit(s, AuditEventType.GRANT, flow_id=flow_id, tool=f"profile:{profile}",
          actor=granted_by,
          principal=principal.email if principal else None,
          details={"grant_id": grant.id, "profile": profile,
                   "tools": list(tools), "window_minutes": window_minutes,
                   "via": granted_via})
    s.commit()
    return grant, token


def mint_forwarding_token(
    s: Session, *, flow_id: str, tool: str, principal: Principal | None,
    ttl_seconds: int = 30,
) -> str:
    """A one-call token the DOOR presents to the proxy (7.3.6).

    The door authorizes a person's call with the Cedar ladder and then
    forwards it into the cluster — through the same sentinel-proxy every
    in-cluster caller uses, never around it, so "nothing reaches an MCP
    server without a capability check" stays literally true. The proxy
    speaks exactly one language: `(flow-id, token, derived scope)`. This
    mints that, scoped to the ONE tool just decided and living ~30
    seconds.

    It costs the human nothing and must not: the approval question was
    already asked and answered upstairs. Two properties keep this from
    being a bypass — the token is minted only AFTER `ladder.decide()`
    returned allowed, and it is scope-locked to that single tool, so a
    leaked one buys a single call it was already entitled to make. The
    kill switch still wins: `check_capability` re-reads it at the proxy,
    so a kill engaged in the milliseconds after minting still stops the
    call."""
    if kill_state(s).engaged:
        raise ValueError("kill switch engaged — no new grants")
    if s.get(Flow, flow_id) is None:
        s.add(Flow(id=flow_id, agent=(f"airlock-door:{principal.email}"
                                     if principal else "sentinel-console")))
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    s.add(CapabilityGrant(
        flow_id=flow_id,
        principal_id=principal.id if principal else None, tool=tool,
        token_hash=_hash(token), granted_by="airlock-door",
        granted_via="admin",
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
    ))
    s.commit()
    return token


def idp_migration_active(s: Session) -> IdpMigration | None:
    """The open migration window, or None. Expiry is checked here (lazy,
    like request expiry) so an abandoned window closes itself."""
    m = s.get(IdpMigration, 1)
    if m is None:
        return None
    if m.expires_at <= utcnow():
        return None
    return m


def _norm_issuer(iss: str | None) -> str:
    """Trailing-slash normalization for issuer comparison ONLY (Authentik
    issuers end in '/', Okta's do not; an operator typo must not open a
    window that silently never matches). Pins store the token's iss
    verbatim; only comparisons normalize."""
    return (iss or "").rstrip("/")


def open_idp_migration(s: Session, *, new_issuer: str, actor: str,
                       ttl_hours: int = 24) -> IdpMigration:
    """ADR-008 D1: the ONE sanctioned email-join across issuers —
    operator-declared, time-boxed, every re-pin individually audited.
    Opening is itself an audited console act.

    A window naming the deployment's CURRENT issuer is REFUSED
    (review-proven attack): migration re-pins only ACROSS issuers.
    Within one issuer, a changed subject stays what ADR-005 D2 made it
    — a permanent anomaly refusal — because a same-issuer window would
    turn the re-issued-mailbox defense into a 24h silent re-bind, the
    exact takeover TOFU pinning exists to stop."""
    from .config import OIDC_ISSUER
    if _norm_issuer(new_issuer) == _norm_issuer(OIDC_ISSUER):
        raise ValueError("same-issuer-window")
    m = s.get(IdpMigration, 1)
    now = utcnow()
    if m is None:
        m = IdpMigration(id=1)
        s.add(m)
    m.new_issuer = new_issuer
    m.opened_by = actor
    m.opened_at = now
    m.expires_at = now + timedelta(hours=ttl_hours)
    audit(s, AuditEventType.POLICY_CHANGE, actor=actor,
          details={"action": "idp-migration-opened",
                   "new_issuer": new_issuer, "ttl_hours": ttl_hours})
    s.commit()
    return m


def close_idp_migration(s: Session, *, actor: str) -> bool:
    """Returns whether a LIVE window was closed — deleting a row lazy
    expiry already ended reports False, so the console never flashes a
    successful close of a window that had already ended itself
    (review-caught). The stale row is removed either way."""
    m = s.get(IdpMigration, 1)
    if m is None:
        return False
    was_live = m.expires_at > utcnow()
    s.delete(m)
    audit(s, AuditEventType.POLICY_CHANGE, actor=actor,
          details={"action": "idp-migration-closed",
                   "new_issuer": m.new_issuer, "was_live": was_live})
    s.commit()
    return was_live


def set_principal_disabled(s: Session, principal_id: str, *,
                           disabled: bool, actor: str) -> Principal:
    """`disabled_at` finally gets a writer (ADR-008 D3 prerequisite —
    the review found the offboarding kill point was a check with no
    trigger). Disabling locks the door immediately: person_from_bearer
    re-reads the row per call, so even an 8h door token dies on its
    next use. Live grants are untouched — revoke them separately if
    the situation calls for it (their check paths are flow/tool-bound
    and short-lived; the PERSON is what this switch turns off)."""
    p = s.get(Principal, principal_id)
    if p is None:
        raise ValueError("unknown-principal")
    p.disabled_at = utcnow() if disabled else None
    audit(s, AuditEventType.POLICY_CHANGE, actor=actor, principal=p.email,
          details={"action": "principal-disabled" if disabled
                   else "principal-enabled"})
    s.commit()
    return p


def get_or_create_principal(
    s: Session, *, email: str, idp_sub: str | None = None,
    idp_iss: str | None = None, idp_stable_id: str | None = None,
    display_name: str | None = None,
) -> Principal:
    """Identity-ledger upsert with ISSUER-QUALIFIED TOFU pinning
    (ADR-005 D2; issuer added by ADR-008 D1 — a bare sub is only
    meaningful relative to who asserted it).

    First sight creates the row (audited — no state without a paper
    trail). The first token carrying an IdP subject pins (iss, sub);
    any later token with the same email and a different subject OR
    issuer is refused and audited — UNLESS an operator has opened the
    IdP migration window for exactly that issuer, in which case the
    row re-pins with its own audit trail (the one sanctioned re-bind).
    Rows minted before the issuer column existed backfill their iss on
    the next matching-sub sign-in, audited, without ceremony. Disabled
    principals are refused the same way: the ledger answers "who", and
    a disabled who is still a no."""
    from sqlalchemy.exc import IntegrityError

    def _collision(pinned_email: str | None = None):
        """The composite unique caught two principals contending for one
        (iss, sub) — an IdP reusing a subject, or an email rename at the
        IdP. Review-proven: without this handler the 500 also rolled the
        audit row back — an identity anomaly with NO paper trail. The
        audit gets a FRESH transaction so the rollback cannot eat it."""
        s.rollback()
        audit(s, AuditEventType.AUTH_FAILURE, principal=email,
              details={"anomaly": "idp-sub-collision", "iss": idp_iss,
                       "sub": idp_sub})
        s.commit()
        raise ValueError("idp-sub-collision")

    email = email.strip().lower()
    p = s.scalars(select(Principal).where(Principal.email == email)).first()
    now = utcnow()
    if p is None:
        p = Principal(email=email, idp_sub=idp_sub, idp_iss=idp_iss,
                      idp_stable_id=idp_stable_id,
                      display_name=display_name, last_seen_at=now)
        s.add(p)
        try:
            s.flush()
        except IntegrityError:
            _collision()
        audit(s, AuditEventType.CREDENTIAL_ADDED, principal=email,
              details={"kind": "principal", "sub_pinned": idp_sub is not None,
                       "iss": idp_iss})
        s.commit()
        return p
    if p.disabled_at is not None:
        audit(s, AuditEventType.AUTH_FAILURE, principal=email,
              details={"anomaly": "principal-disabled"})
        s.commit()
        raise ValueError("principal-disabled")
    if idp_sub is not None:
        sub_mismatch = p.idp_sub is not None and p.idp_sub != idp_sub
        iss_mismatch = (p.idp_iss is not None and idp_iss is not None
                        and _norm_issuer(p.idp_iss) != _norm_issuer(idp_iss))
        if p.idp_sub is None:
            p.idp_sub, p.idp_iss = idp_sub, idp_iss
            p.idp_stable_id = idp_stable_id or p.idp_stable_id
            audit(s, AuditEventType.CREDENTIAL_ADDED, principal=email,
                  details={"kind": "principal-sub-pin", "iss": idp_iss})
        elif sub_mismatch or iss_mismatch:
            m = idp_migration_active(s)
            if m is not None and _norm_issuer(idp_iss) == _norm_issuer(m.new_issuer):
                old_iss, old_sub = p.idp_iss, p.idp_sub
                p.idp_iss, p.idp_sub = idp_iss, idp_sub
                p.idp_stable_id = idp_stable_id
                audit(s, AuditEventType.CREDENTIAL_ADDED, principal=email,
                      details={"kind": "principal-sub-repin",
                               "old_iss": old_iss, "old_sub": old_sub,
                               "new_iss": idp_iss,
                               "migration_opened_by": m.opened_by})
            else:
                # near-miss visibility (review-caught): if a window IS
                # open, name its issuer in the refusal so an operator
                # mid-migration can see the almost-match instead of a
                # generic anomaly.
                details = {"anomaly": "idp-sub-mismatch",
                           "pinned_iss": p.idp_iss, "token_iss": idp_iss}
                if m is not None:
                    details["open_window_issuer"] = m.new_issuer
                audit(s, AuditEventType.AUTH_FAILURE, principal=email,
                      details=details)
                s.commit()
                raise ValueError("idp-sub-mismatch")
        elif p.idp_iss is None and idp_iss is not None:
            # pre-b6e4d1a8c3f2 row: same sub, issuer column empty —
            # backfill, audited, no ceremony.
            p.idp_iss = idp_iss
            p.idp_stable_id = p.idp_stable_id or idp_stable_id
            audit(s, AuditEventType.CREDENTIAL_ADDED, principal=email,
                  details={"kind": "principal-iss-backfill", "iss": idp_iss})
        elif idp_stable_id and p.idp_stable_id is None:
            p.idp_stable_id = idp_stable_id
    if display_name and p.display_name != display_name:
        p.display_name = display_name
    p.last_seen_at = now
    try:
        s.commit()
    except IntegrityError:
        _collision()
    return p
