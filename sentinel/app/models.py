"""Data model (filled in by checklist item 5.5.2).

Three tables, per the phase doc:
  - flows:             id, agent, started_at, ended_at, metadata (JSON)
  - capability_grants: id, flow_id, tool, granted_at, expires_at,
                       granted_by, revoked_at
  - audit_events:      id, ts, event_type (request/grant/denial/use/
                       revocation), flow_id, tool, actor, details (JSON)

This module already imports Base so Alembic's env.py has a stable
metadata target from day one — the first real migration is 5.5.2's.
"""

from .db import Base  # noqa: F401  (metadata anchor for Alembic)
