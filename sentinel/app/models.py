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

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String
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


class AuditEventType(StrEnum):
    REQUEST = "request"
    GRANT = "grant"
    DENIAL = "denial"
    USE = "use"
    REVOCATION = "revocation"


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
