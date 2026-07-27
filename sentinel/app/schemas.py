"""API request/response shapes. These Field descriptions and examples
ARE the human documentation — FastAPI renders them at /docs on the
admin listener, so they can never drift from the code.

Convention: all timestamps are UTC, serialized ISO-8601 with a `Z`
suffix (the DB stores naive UTC; the `Z` is appended at the edge).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

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
        max_length=128,
        description="Who is asking (informational, audited).",
        examples=["control-plane-claude"],
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


class GrantIn(SentinelModel):
    ttl_minutes: int = Field(
        default=5, ge=1, le=1440,
        description="Grant lifetime. The GUI offers 5 (default) and 60.")
    granted_by: str = Field(
        max_length=128,
        description="Human identity making the call (bound to WebAuthn at 5.5.6).",
        examples=["bob"],
    )


class GrantOut(SentinelModel):
    request_id: str
    status: str
    grant_id: str
    expires_at: datetime = Field(description="Grant expiry, not request expiry.")
    note: str = Field(
        default="Token is delivered to the requester via its status poll — "
                "it is never shown here.",
        description="Why there is no token in this response.")


class DenyIn(SentinelModel):
    denied_by: str = Field(max_length=128, examples=["bob"])
    reason: str | None = Field(
        default=None, max_length=512,
        description="Optional; delivered verbatim to the requester's poll.")


class KillIn(SentinelModel):
    engaged_by: str = Field(max_length=128, examples=["bob"])
    reason: str | None = Field(default=None, max_length=512)


class ReleaseIn(SentinelModel):
    released_by: str = Field(max_length=128, examples=["bob"])


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
