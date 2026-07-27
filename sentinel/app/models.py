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

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """Naive UTC now — the single timestamp convention for the DB."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


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

    grants: Mapped[list["CapabilityGrant"]] = relationship(back_populates="flow")


class CapabilityGrant(Base):
    """A human's yes: one tool, one flow, one expiry. Scope-locked by
    construction — /capability-check matches tool AND flow_id AND
    token hash, so a token can never be replayed across flows or
    tools."""

    __tablename__ = "capability_grants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    flow_id: Mapped[str] = mapped_column(
        ForeignKey("flows.id"), index=True
    )  # real FK: grants exist only for registered flows
    tool: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    granted_by: Mapped[str] = mapped_column(String(128))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)

    flow: Mapped[Flow] = relationship(back_populates="grants")


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
