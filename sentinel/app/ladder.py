"""The four-outcome ladder (7.2.3, ADR-005 Decision 4) — the broker's
decision procedure for a PERSON's tool call.

Two questions, deliberately separated:

  1. What does POLICY say this person could do here?  Pure Cedar,
     evaluated up to three times with hypothetical contexts —
     baseline ⇒ permit · elevated ⇒ confirm · approved ⇒ approve ·
     none ⇒ forbid. Explicit `forbid` policies override every rung
     (engine property), so possession of a grant can never unlock a
     forbidden action.
  2. What does this person HOLD?  A live grant whose mint-time
     snapshot covers the tool turns `confirm`/`approve` into a yes;
     without one, the answer is a 403 that says exactly what borrowing
     would take (profile + windows) — the client-facing elevation
     offer. Which grants satisfy which rung is a strength ordering:
     any live covering grant satisfies `elevated`; only grants a
     HUMAN issued on the console (`granted_via` approve or admin — the
     5.5 card flow is Airlock's approve door) satisfy `approved`.

Order of checks mirrors check_capability: kill first (fail closed
beats fast), then policy. No active policy store ⇒ the person path
denies closed — an unconfigured Airlock grants nothing.

Callers: 7.3's gateway door (HTTP). Until that exists this module is
exercised by tests and carries no route. Every decision — either
verdict — audits with principal, resource, and the policy version
that decided it (ADR-005 D3's reconstruction requirement).
"""

import json
from dataclasses import dataclass

from cedarpy import Decision, is_authorized
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import policy
from .models import AuditEventType, CapabilityGrant, Principal, utcnow
from .service import _grant_covers, audit, kill_state

# granted_via values that satisfy the `approved` rung: a human on the
# passkey console said yes (admin = the 5.5 card flow, approve = the
# same flow reached through 7.3's door). Self-elevation (`confirm`)
# satisfies only `elevated`.
_HUMAN_ISSUED = {"approve", "admin"}


@dataclass(frozen=True)
class LadderResult:
    allowed: bool
    outcome: str            # permit | confirm | approve | forbid | error-reason
    reason: str             # "ok" or the deny reason (stable strings, audited)
    resource: str | None = None
    grant_id: str | None = None
    hint: dict | None = None  # the elevation offer, when borrowing would work


def _ask(ap, email: str, action: str, resource_id: str, server: str,
         tier: str, context: dict) -> bool:
    entities = json.loads(ap.entities_json) + [{
        "uid": {"type": "Resource", "id": resource_id},
        "attrs": {"server": server, "tier": tier}, "parents": [],
    }]
    req = {"principal": f'User::"{email}"',
           "action": f'Action::"{action}"',
           "resource": f'Resource::"{resource_id}"',
           "context": context}
    return is_authorized(req, ap.policies, entities).decision == Decision.Allow


def _windows(ap, email: str, server: str, level: str) -> list[int]:
    """Which windows the elevation offer may name: the matrix cell that
    would grant, found through the person's transitive groups."""
    person = ap.people.get(email)
    direct = set((person or {}).get("groups") or []) | {policy.BIRTHRIGHT_GROUP}
    defaults = (ap.matrix.get("defaults") or {}).get(
        "windows", policy.DEFAULT_WINDOWS)
    for g in sorted(policy.transitive_groups(ap.groups, direct)):
        cell = ((ap.matrix.get("grants") or {}).get(g) or {}).get(server)
        if cell and cell.get("level") == level:
            return cell.get("windows", defaults)
    return defaults


def _deny(s: Session, *, email: str, tool: str, reason: str, outcome: str,
          resource: str | None, version: str | None,
          hint: dict | None = None) -> LadderResult:
    audit(s, AuditEventType.DENIAL, tool=tool, principal=email,
          resource=resource, policy_version=version,
          details={"source": "ladder", "outcome": outcome, "reason": reason,
                   **({"hint": hint} if hint else {})})
    s.commit()
    return LadderResult(allowed=False, outcome=outcome, reason=reason,
                        resource=resource, hint=hint)


def decide(s: Session, *, principal: Principal, tool: str,
           arguments: dict | None = None) -> LadderResult:
    """May this person make this call, right now? `tool` is scope.py's
    composite (`<server>.<leaf>`); `arguments` is the JSON-RPC
    params.arguments record (None for handshake scopes)."""
    email = principal.email

    if kill_state(s).engaged:
        return _deny(s, email=email, tool=tool, reason="kill-engaged",
                     outcome="forbid", resource=None, version=None)

    ap = policy.get_active()
    if ap is None:
        # An unconfigured Airlock grants nothing — and says so in a way
        # the console's status panel can explain.
        return _deny(s, email=email, tool=tool, reason="no-policy",
                     outcome="forbid", resource=None, version=None)

    server, _, leaf = tool.partition(".")
    if not leaf:
        return _deny(s, email=email, tool=tool, reason="unmapped-path",
                     outcome="forbid", resource=None, version=ap.version)

    action = policy.classify_tool(ap.servers, server, leaf)
    if action is None:
        # Not in the server's declared read/write sets: the platform
        # does not guess whether an unknown verb is dangerous.
        return _deny(s, email=email, tool=tool, reason="unclassified-tool",
                     outcome="forbid", resource=None, version=ap.version)

    derived = policy.derive_resource(ap.servers, server, leaf, arguments)
    if derived is None:
        return _deny(s, email=email, tool=tool, reason="unmapped-resource",
                     outcome="forbid", resource=None, version=ap.version)
    resource_id, tier = derived

    def ask(ctx: dict) -> bool:
        return _ask(ap, email, action, resource_id, server, tier, ctx)

    if ask({}):
        outcome = "permit"
    elif ask({"elevated": True}):
        outcome = "confirm"
    elif ask({"approved": True}):
        outcome = "approve"
    else:
        return _deny(s, email=email, tool=tool, reason="forbidden",
                     outcome="forbid", resource=resource_id,
                     version=ap.version)

    grant = None
    if outcome != "permit":
        now = utcnow()
        live = s.scalars(select(CapabilityGrant).where(
            CapabilityGrant.principal_id == principal.id,
            CapabilityGrant.revoked_at.is_(None),
            CapabilityGrant.expires_at > now,
        )).all()
        covering = [g for g in live if _grant_covers(g, tool)]
        if outcome == "approve":
            covering = [g for g in covering if g.granted_via in _HUMAN_ISSUED]
        if not covering:
            level = ("write-on-request" if outcome == "confirm"
                     else "write-on-approval")
            hint = {"profile": f"{server}:write",
                    "windows": _windows(ap, email, server, level)}
            reason = ("elevation-available" if outcome == "confirm"
                      else "approval-required")
            return _deny(s, email=email, tool=tool, reason=reason,
                         outcome=outcome, resource=resource_id,
                         version=ap.version, hint=hint)
        grant = covering[0]

    audit(s, AuditEventType.USE, flow_id=grant.flow_id if grant else None,
          tool=tool, principal=email, resource=resource_id,
          policy_version=ap.version,
          details={"source": "ladder", "outcome": outcome,
                   **({"grant_id": grant.id, "profile": grant.profile}
                      if grant else {"path": "birthright"})})
    s.commit()
    return LadderResult(allowed=True, outcome=outcome, reason="ok",
                        resource=resource_id,
                        grant_id=grant.id if grant else None)
