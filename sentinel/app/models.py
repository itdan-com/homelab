"""Sentinel's data model (checklist 5.5.2) — three tables.

Conventions that everything else builds on:

- **Time**: naive UTC datetimes everywhere in the DB (`utcnow()`).
  SQLite has no timezone type; mixing aware and naive datetimes is a
  comparison-bug factory, so the convention is naive-UTC in, naive-UTC
  out, and the API layer renders ISO-8601 with a Z.
- **Identifiers**: `flows.id` is the CLIENT-SUPPLIED flow-id (Claude
  mints one per task — CLAUDE.md "per-flow ephemeral capabilities").
  Grant ids are random UUID hex: anything that crosses a trust
  boundary must be unguessable; autoincrement would leak count/order.
  Audit ids are autoincrement on purpose — internal only, and the
  ordering is a feature for a log.
- **Secrets**: tokens are NEVER stored. `capability_grants.token_hash`
  holds a SHA-256 hex digest; /capability-check (5.5.3) hashes the
  presented token and compares. A stolen sentinel.db yields no usable
  credentials. (This column is a deliberate schema-forward addition to
  the phase doc's minimal list — recorded in the phase notes.)
- **A grant is valid iff**: `revoked_at IS NULL` AND `utcnow() <
  expires_at` AND the global kill switch is off (kill state lives
  outside this table — 5.5.3).
"""

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """Naive UTC now — the single timestamp convention for the DB."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class Principal(Base):
    """A PERSON, as Airlock knows one (7.2.1, ADR-005 Decision 2).

    This is the runtime identity LEDGER, not the authorization source:
    WHO a principal is (email, the TOFU-pinned IdP subject) lives here;
    what their groups entitle lives in the policy store the console
    edits (ADR-005 Decision 5). The DB never answers "may they" — only
    "who was it". Rows are created on first authenticated contact and
    never deleted (they anchor grants and audit rows); offboarding is
    `disabled_at`, and entitlement removal is a policy-store edit."""

    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    # Pinned on first sight (trust-on-first-use). A later token carrying
    # the same email with a DIFFERENT subject is a named anomaly and a
    # refusal, not a silent re-bind — the cheap defense against an IdP
    # re-issuing an address to a new hire (ADR-005 Decision 2).
    idp_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)


class Flow(Base):
    """One Claude task. Created at first /capability-request for an
    unseen flow-id; `ended_at` closes it (flows end, rows never delete —
    they anchor the audit trail)."""

    __tablename__ = "flows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Attribute is `meta` because `metadata` is reserved on declarative
    # classes (it's the table registry) — the COLUMN keeps the phase
    # doc's name.
    meta: Mapped[dict | None] = mapped_column("metadata", JSON)
    # 7.2.1 (ADR-004 debt 1): whose task this is. NULL = the
    # pre-multi-user domain — in-cluster agent callers behind mTLS,
    # where the flow-id namespace is effectively single-tenant.
    # Person-flows (7.3's gateway door) always carry a principal, and
    # their flow ids are GATEWAY-MINTED — which is what retires the
    # "two users' flow-1 collide" debt without re-keying this table:
    # client-chosen ids only ever existed in the single-tenant domain.
    principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id"), index=True
    )

    grants: Mapped[list["CapabilityGrant"]] = relationship(back_populates="flow")


class CapabilityGrant(Base):
    """A yes: one tool — or, since 7.2.1, a PROFILE (a named tool-set
    snapshot) — for one window. Scope-locked by construction:
    /capability-check matches token hash AND flow AND tool coverage,
    so a token can never be replayed across flows or outside its set.

    Profile grants (ADR-005 Decision 6): `profile` names the set,
    `tools_json` SNAPSHOTS the member tools at mint time — a later
    policy-store edit must not retroactively widen a live grant —
    and `tool` holds the sentinel value `profile:<name>` (kept NOT
    NULL so nothing downstream learns a new None case). `granted_via`
    records the door: admin (console card), confirm (self-elevation),
    approve (a different human) — the ADR-005 carve, visible per row."""

    __tablename__ = "capability_grants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # NULL for principal-bound profile grants (their door is 7.3);
    # every per-flow grant sets it, and the check path denies a
    # flow-less grant outright until the person-door exists.
    flow_id: Mapped[str | None] = mapped_column(ForeignKey("flows.id"), index=True)
    principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id"), index=True
    )
    tool: Mapped[str] = mapped_column(String(128))
    profile: Mapped[str | None] = mapped_column(String(64))
    tools_json: Mapped[list | None] = mapped_column(JSON)
    granted_via: Mapped[str] = mapped_column(String(16), default="admin")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    granted_by: Mapped[str] = mapped_column(String(128))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)

    flow: Mapped[Flow | None] = relationship(back_populates="grants")
    principal: Mapped[Principal | None] = relationship()


class RequestStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


class CapabilityRequest(Base):
    """A pending question to the human: "may flow X use tool Y?".

    Persisted (not an in-memory channel) so a Sentinel restart loses
    nothing, the GUI can list it, and the audit story has an anchor.
    Requests auto-expire (default 10 min): `expired` is computed
    lazily at read time — no background sweeper in the MVP.
    Duplicate (flow_id, tool) requests while one is pending dedupe
    onto the existing row, so client retries don't spam the GUI.
    """

    __tablename__ = "capability_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    flow_id: Mapped[str] = mapped_column(ForeignKey("flows.id"), index=True)
    tool: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(512))
    status: Mapped[RequestStatus] = mapped_column(
        Enum(
            RequestStatus,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda e: [m.value for m in e],
            length=16,
        ),
        default=RequestStatus.PENDING,
        index=True,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[str | None] = mapped_column(String(128))
    denied_reason: Mapped[str | None] = mapped_column(String(512))
    grant_id: Mapped[str | None] = mapped_column(ForeignKey("capability_grants.id"))
    # Token delivery channel: the plaintext lives here ONLY between
    # grant and the requester's first successful poll (claim-once —
    # nulled on claim, and nulled if the grant lapses unclaimed). The
    # admin who grants never sees it; after claim, only the hash in
    # capability_grants remains anywhere. Consequence for backups: a
    # stolen DB can contain at most in-flight unclaimed tokens, each
    # scope-locked and minutes from expiry.
    token_plaintext: Mapped[str | None] = mapped_column(String(128))
    token_claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # SHA-256 of a secret the REQUESTER minted and kept. It is what makes
    # the one-time pickup belong to the caller that asked: without it,
    # dedupe handed any caller another caller's request_id and the poll
    # authenticated nothing, so whoever polled fastest after the human
    # clicked Grant took the token. Nullable only because rows predating
    # 5.5.5 have none (all long resolved).
    claim_nonce_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # 7.2.1: elevation requests (the Airlock doors, 7.3) name a PROFILE
    # and a WINDOW instead of a single tool; the person asking rides
    # principal_id. All nullable — agent-flow requests predate them.
    principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id"), index=True
    )
    profile: Mapped[str | None] = mapped_column(String(64))
    window_minutes: Mapped[int | None] = mapped_column()

    flow: Mapped[Flow] = relationship()
    grant: Mapped["CapabilityGrant | None"] = relationship()


class KillState(Base):
    """The global kill switch — a singleton row (id=1), CURRENT state
    only (history lives in audit_events). Persisted so that a Sentinel
    restart while killed COMES BACK killed: fail-closed must survive
    crashes, not just uptime."""

    __tablename__ = "kill_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    engaged: Mapped[bool] = mapped_column(default=False)
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime)
    engaged_by: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(String(512))
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    released_by: Mapped[str | None] = mapped_column(String(128))


class AuditEventType(StrEnum):
    REQUEST = "request"
    GRANT = "grant"
    DENIAL = "denial"
    USE = "use"
    REVOCATION = "revocation"
    # Human-auth transitions (5.5.6). The record must be able to answer
    # "who could have approved this, and when did that change" — an
    # authenticator being added is as security-relevant as a grant.
    AUTH_SUCCESS = "auth_success"
    AUTH_FAILURE = "auth_failure"
    CREDENTIAL_ADDED = "credential_added"
    # Two additions over the phase doc's five (recorded in its notes):
    # kill-switch transitions are security-critical events in their own
    # right — hiding them inside "revocation" would blur the audit story.
    KILL_ENGAGED = "kill_engaged"
    KILL_RELEASED = "kill_released"
    # The moment the credential leaves Sentinel. Without it the record
    # cannot answer "was this capability ever actually picked up?" —
    # and a grant that was never claimed looks identical to one that
    # was, which is the wrong thing to be unsure about after an incident.
    CLAIM = "claim"
    # 7.2.4: a policy-store activation attempt — result `activated` or
    # `rejected` in details. Rejections are recorded on purpose: a
    # stream of rejected saves is somebody probing the policy surface,
    # and that is exactly what the record exists to show. The DIFF is
    # deliberately not in the row — the store's own git history holds
    # it, keyed by the versions this row names.
    POLICY_CHANGE = "policy_change"


class AuditEvent(Base):
    """Append-only canonical record. flow_id/tool/actor are plain
    strings with NO foreign keys, on purpose: the audit log must also
    record garbage — unknown flow-ids, refused requests, malformed
    calls. An audit insert that can fail referential integrity is an
    audit log an attacker can silence."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(
            AuditEventType,
            native_enum=False,  # SQLite: VARCHAR...
            create_constraint=True,  # ...plus a CHECK so the DB rejects junk
            values_callable=lambda e: [m.value for m in e],  # store "grant", not "GRANT"
            length=16,
        )
    )
    flow_id: Mapped[str | None] = mapped_column(String(64), index=True)
    tool: Mapped[str | None] = mapped_column(String(128))
    actor: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict | None] = mapped_column(JSON)
    # 7.2.1 (ADR-005 Decision 3): who / on what / under which policy.
    # Plain strings like everything else here — the audit log records
    # garbage too, so no FKs. `principal` is the email as presented;
    # `resource` arrives with the Cedar work (7.2.3); `policy_version`
    # stamps the policy-store version that decided the row.
    principal: Mapped[str | None] = mapped_column(String(254), index=True)
    resource: Mapped[str | None] = mapped_column(String(255))
    policy_version: Mapped[str | None] = mapped_column(String(64))
    # ADR-007 Decision 1 — the velocity signal. Populated only on the
    # ladder's USE row (the only event type velocity counts): the
    # RESOURCE column already holds the concrete resource_id ("wipe
    # laptop A" vs "wipe laptop B"), which is exactly what a bulk-action
    # count must NOT key on — a hundred different laptops would each
    # count as one. Tier is the axis a velocity rule actually wants
    # ("5 deletes/hour on prod, 50/hour on staging"), and it rides the
    # same classification `resource.tier` already carries in every
    # Cedar evaluation.
    tier: Mapped[str | None] = mapped_column(String(64))
    # 7.6 — tamper evidence. The chain is computed by a SEALING pass,
    # not on insert: audit() is called on every hot path by three
    # separate processes, and making each write first read the previous
    # row would turn the record into a contention point (and two
    # processes reading the same predecessor would fork the chain).
    # Unsealed rows are still the truth; they are simply not yet
    # provable. See app/audit_chain.py.
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    row_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    # ADR-007 Decision 1: the exact index the velocity count's hot-path
    # query needs. This now runs before EVERY person-path decide() call,
    # not just forensics — an unindexed scan here would be the hot-path
    # latency regression the design was careful to avoid.
    __table_args__ = (
        Index("ix_audit_events_velocity", "principal", "tool", "tier", "ts"),
    )


# --- human auth (5.5.6) -------------------------------------------------------
#
# Until now, "the human" was a config string and loopback-reachability
# was the whole authorization model. These four tables replace that with
# a credential the operator physically holds. Same secret discipline as
# everywhere else: nothing here stores anything replayable — public keys
# are public by definition, sessions and enrollment codes are SHA-256
# digests, and the TOTP secret is the one unavoidable exception (it is
# shared-secret by construction, which is exactly why it is the fallback
# and not the primary).


class Operator(Base):
    """A human who may approve. Deliberately NOT synced from the
    platform's identity provider: Authentik runs inside the cluster this
    service polices, and its groups are declared in the same git repo the
    agent opens PRs against (ADR-004). Who may APPROVE is a small set
    managed here; who may USE the platform is a different question with a
    different answer."""

    __tablename__ = "operators"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Base32 TOTP seed, present only if the operator enrolled the
    # fallback. NULL for passkey-only operators, which is the better
    # posture — a TOTP seed is phishable in a way a passkey is not.
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class WebAuthnCredential(Base):
    """One registered authenticator. An operator may hold several —
    laptop, phone, hardware key — and that IS the recovery story: a
    second passkey beats any account-recovery backdoor, because a
    backdoor is a second front door to the kill switch."""

    __tablename__ = "webauthn_credentials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), index=True)
    label: Mapped[str] = mapped_column(String(128))
    # Base64url credential id as the browser reports it, and the COSE
    # public key. Neither is a secret; the private key never leaves the
    # authenticator, which is the entire point of choosing WebAuthn.
    credential_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    # Cloned-authenticator detector: a counter that goes backwards means
    # two devices are presenting the same credential.
    sign_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)

    operator: Mapped[Operator] = relationship()


class ConsoleSession(Base):
    """A verified browser session. Stored server-side (hash only) rather
    than as a self-contained signed cookie, so that revocation is real:
    a stolen laptop is answered by deleting rows, not by waiting for an
    expiry the attacker's copy also carries."""

    __tablename__ = "console_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    method: Mapped[str] = mapped_column(String(16))  # webauthn | totp

    operator: Mapped[Operator] = relationship()


class EnrollmentCode(Base):
    """Out-of-band authorization to add an authenticator, minted by
    `scripts/enroll-operator.sh` on the host and printed to the terminal.

    Registration must not be self-service just because no credential
    exists yet: 'first browser to reach the port wins' would mean any
    local process — or anything that talks a victim's browser into a
    request — could enroll itself as the approver. Requiring a code from
    the host's shell makes enrolling a deliberate act by someone who
    already has the host."""

    __tablename__ = "enrollment_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class WebAuthnChallenge(Base):
    """A pending ceremony challenge. Server-side and single-use: a
    challenge kept in the browser's session would let a replayed one be
    reused, and the whole point of the challenge is that it cannot."""

    __tablename__ = "webauthn_challenges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    purpose: Mapped[str] = mapped_column(String(16))  # register | login
    operator_id: Mapped[str | None] = mapped_column(String(32))
    enrollment_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
