"""API request/response shapes. These Field descriptions and examples
ARE the human documentation — FastAPI renders them at /docs on the
admin listener, so they can never drift from the code.

Convention: all timestamps are UTC, serialized ISO-8601 with a `Z`
suffix (the DB stores naive UTC; the `Z` is appended at the edge).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from .config import DEFAULT_GRANT_TTL_MINUTES, MAX_GRANT_TTL_MINUTES

FLOW_ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
TOOL_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"


class SentinelModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json")
    def _utc_z(self, v, _info):
        if isinstance(v, datetime):
            return v.isoformat() + "Z"
        return v


# --- broker (cluster-facing) --------------------------------------------------

class CapabilityRequestIn(SentinelModel):
    flow_id: str = Field(
        pattern=FLOW_ID_PATTERN,
        description="Caller-minted id of the running task. Capabilities are "
                    "scope-locked to it: a token granted for this flow is "
                    "useless from any other flow.",
        examples=["flow-7f3a2b"],
    )
    tool: str = Field(
        pattern=TOOL_PATTERN,
        description="Exact MCP tool name the capability is for (one tool per "
                    "grant — request twice for two tools).",
        examples=["github.create_pr"],
    )
    reason: str = Field(
        max_length=512,
        description="Plain-English justification, shown verbatim to the human "
                    "on the approval screen. Write it for them.",
        examples=["Open the PR for the KEDA ceiling change discussed in #ops"],
    )
    agent: str = Field(
        default="unknown",
        pattern=r"^[A-Za-z0-9 ._-]{1,128}$",
        description="Who is asking (informational, audited). Pattern-bound "
                    "like flow_id and tool: this string is displayed on the "
                    "approval screen, and the screen that holds the kill "
                    "switch takes no markup from the thing asking for power.",
        examples=["control-plane-claude"],
    )
    claim_nonce: str = Field(
        min_length=16, max_length=128,
        description="A secret YOU generate and keep (e.g. secrets.token_urlsafe(32)). "
                    "Sentinel stores only its hash and requires it back on the "
                    "status poll, so the one-time token can only be picked up by "
                    "the caller that asked — not by whoever polls fastest after "
                    "the human clicks Grant.",
        examples=["e2Fk9wQ7nS1pXyL0vB4tRc8mZ6hJ3aUd"],
    )


class CapabilityRequestOut(SentinelModel):
    request_id: str = Field(description="Poll GET /v1/capability-requests/{request_id} with this.")
    status: str = Field(description="pending | granted | denied | expired")
    flow_id: str
    tool: str
    requested_at: datetime
    expires_at: datetime = Field(description="When a still-pending request lapses.")


class CapabilityRequestStatus(CapabilityRequestOut):
    token: str | None = Field(
        default=None,
        description="THE capability token — present exactly once: on the first "
                    "poll after a grant (claim-once delivery; Sentinel keeps "
                    "only a hash from then on). Store it in memory, send it as "
                    "X-Sentinel-Token, never log it. If you lose it, request "
                    "again.",
    )
    grant_expires_at: datetime | None = Field(
        default=None, description="When the granted capability stops working.")
    denied_reason: str | None = None


class CheckAllowed(SentinelModel):
    allowed: bool = Field(description="True — and the HTTP status is 200.")
    grant_id: str
    flow_id: str
    tool: str
    expires_at: datetime


class CheckDenied(SentinelModel):
    allowed: bool = Field(description="False — and the HTTP status is 403.")
    reason: str = Field(
        description="kill-engaged | unknown-token | scope-mismatch | revoked | expired")


# --- admin (loopback-only) ----------------------------------------------------

class PendingRequest(SentinelModel):
    request_id: str
    flow_id: str
    agent: str = Field(description="From the flow record.")
    tool: str
    reason: str
    requested_at: datetime
    expires_at: datetime


class AdminAction(SentinelModel):
    """Base for admin request bodies. `extra="forbid"` on purpose: the
    actor fields (`granted_by`, `denied_by`, …) USED to live here and
    now come from the server (app.actor). Rejecting them loudly beats
    accepting and ignoring them — a caller who thinks it is naming the
    approver would otherwise be silently wrong in the audit log."""
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class GrantIn(AdminAction):
    ttl_minutes: int = Field(
        default=DEFAULT_GRANT_TTL_MINUTES, ge=1, le=MAX_GRANT_TTL_MINUTES,
        description="Grant lifetime. The console offers 5 (default) and 60. "
                    "Default and ceiling are config, not literals — a systemd "
                    "unit that shrinks SENTINEL_GRANT_TTL_MINUTES to reduce "
                    "blast radius used to be silently ignored.")


class GrantOut(SentinelModel):
    request_id: str
    status: str
    grant_id: str
    expires_at: datetime = Field(description="Grant expiry, not request expiry.")
    note: str = Field(
        default="Token is delivered to the requester via its status poll — "
                "it is never shown here.",
        description="Why there is no token in this response.")


class DenyIn(AdminAction):
    reason: str | None = Field(
        default=None, max_length=512,
        description="Optional; delivered verbatim to the requester's poll.")


class KillIn(AdminAction):
    reason: str | None = Field(default=None, max_length=512)


class KillStatus(SentinelModel):
    engaged: bool
    engaged_at: datetime | None = None
    engaged_by: str | None = None
    reason: str | None = None
    released_at: datetime | None = None
    released_by: str | None = None
    grants_revoked: int | None = Field(
        default=None,
        description="Set on the engage response: live grants revoked by this kill. "
                    "Kill is revocation, not pause — released systems need NEW grants.")


class AuditEventOut(SentinelModel):
    id: int
    ts: datetime
    event_type: str
    flow_id: str | None
    tool: str | None
    actor: str | None
    details: dict | None


class FlowOut(SentinelModel):
    id: str
    agent: str
    started_at: datetime
    ended_at: datetime | None
    # Derived, not stored: nothing closes a flow yet (an agent would have
    # to say so), so "is it active" has to be answered from evidence —
    # a live grant, or recent activity in the audit log.
    last_seen: datetime | None = Field(
        default=None, description="Timestamp of this flow's most recent audit event.")
    live_grants: int = Field(
        default=0, description="Grants that are neither expired nor revoked right now.")
    pending_requests: int = Field(
        default=0, description="Requests still waiting on a human.")
