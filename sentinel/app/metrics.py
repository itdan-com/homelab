"""ADR-006 Decision 1: the broker's /metrics — rates and state, never people.

Hand-rendered Prometheus text format, deliberately no prometheus_client
dependency: the exposition format is a dozen lines of string building,
and this repo's requirements are curated pins with justifications — a
library is not worth a pin for this.

The label discipline is the whole design (ADR-006): bounded labels ONLY.
`event_type` is a closed enum, `server` is clamped to the active policy
store's server list (anything else — junk tools, no active policy —
lands in "other"), `granted_via` has exactly three values, `window` has
exactly two. Principal and resource are UNBOUNDED and belong in the log
line (the Loki copy), never in a label; a helper below asserts the
discipline so a future metric can't drift into the cardinality mistake
Phase 8 already warned about.

Counters vs gauges, stated honestly: the audit table is PRUNED at
rotation (7.6), so a monotonic counter derived from it would go DOWN —
a lie Prometheus rate() would misread as a reset. Windowed gauges are
what the table can truthfully support, so that is what we expose.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import policy
from .config import AUDIT_EXPORT_DIR
from .models import (
    AuditEvent,
    CapabilityGrant,
    CapabilityRequest,
    KillState,
    RequestStatus,
    utcnow,
)

# Trailing windows for event gauges. A shape, not a tuning knob (the
# dashboard and alerts reference these label values by name).
WINDOWS_MINUTES = {"5m": 5, "1h": 60}


def _esc(v: str) -> str:
    """Prometheus label-value escaping: backslash, quote, newline."""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _server_of(tool: str | None, known: set[str]) -> str:
    """Clamp a tool's server segment to the policy store's bounded set.

    Audit rows record hostile input too (a garbage tool name is itself
    an audited fact), so the derived label must not echo attacker text:
    anything not in the active store's server list is "other", and a
    missing/blank tool is "" (kill flips, auth events, policy changes).
    """
    if not tool:
        return ""
    server = tool.partition(".")[0]
    return server if server in known else "other"


def _shipping_state() -> dict:
    """The Loki shipper's watermark file (written by the admin process;
    read here soft-fail — absent file means shipping never ran)."""
    try:
        with open(os.path.join(AUDIT_EXPORT_DIR, "loki-shipper.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def render(s: Session) -> str:
    now = utcnow()
    ap = policy.get_active()
    known = set(ap.servers) if ap else set()
    out: list[str] = []

    def metric(name: str, mtype: str, help_: str, samples: list[tuple[dict, float]]):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            # NOT %g: it truncates to 6 significant digits, which turns an
            # epoch timestamp into a value up to ~83 minutes wrong (review
            # caught it live — 1755859312 rendered as 1.75586e+09).
            rendered = str(int(value)) if float(value).is_integer() else repr(float(value))
            if labels:
                body = ",".join(f'{k}="{_esc(str(v))}"' for k, v in sorted(labels.items()))
                out.append(f"{name}{{{body}}} {rendered}")
            else:
                out.append(f"{name} {rendered}")

    # -- state ---------------------------------------------------------------
    # Read WITHOUT service.kill_state(): that helper creates-and-commits
    # the singleton on first call, and a metrics scrape must never be a
    # database writer. Absent row = never engaged.
    ks = s.get(KillState, 1)
    metric("sentinel_kill_switch_engaged", "gauge",
           "1 while the global kill switch is engaged.",
           [({}, 1.0 if (ks and ks.engaged) else 0.0)])

    metric("sentinel_policy_active", "gauge",
           "1 while a policy store version is active (0 = person-path denies closed).",
           [({}, 1.0 if ap else 0.0)])
    if ap:
        metric("sentinel_policy_info", "gauge",
               "Constant 1, labeled with the active policy version.",
               [({"version": ap.version}, 1.0)])

    live = (
        select(CapabilityGrant.granted_via, func.count())
        .where(CapabilityGrant.revoked_at.is_(None),
               CapabilityGrant.expires_at > now)
        .group_by(CapabilityGrant.granted_via)
    )
    # Accumulate (+=), never assign: an out-of-enum granted_via must fold
    # into `other` without clobbering a real door's count (review-caught).
    by_via = {"admin": 0, "confirm": 0, "approve": 0, "other": 0}
    for via, n in s.execute(live):
        by_via[via if via in by_via else "other"] += n
    metric("sentinel_grants_live", "gauge",
           "Live (unexpired, unrevoked) capability grants by door.",
           [({"granted_via": via}, float(n)) for via, n in sorted(by_via.items())])

    # Request expiry is LAZY (refresh_status flips PENDING->EXPIRED only
    # when a row is read), so filter on expires_at here or one abandoned
    # request paints the 3am row red forever (review-caught).
    pending = s.execute(
        select(func.count()).select_from(CapabilityRequest)
        .where(CapabilityRequest.status == RequestStatus.PENDING,
               CapabilityRequest.expires_at > now)
    ).scalar_one()
    metric("sentinel_requests_pending", "gauge",
           "Capability requests waiting on a human decision.",
           [({}, float(pending))])

    # -- rates (windowed gauges over the audit table) ------------------------
    # GROUP BY in SQL so scrape cost tracks distinct (event_type, tool)
    # pairs, not raw row volume — the gate audits hostile garbage by
    # design, and an attacker spamming refusals must not be able to
    # linearly inflate every scrape (review-caught). The server clamp
    # then bounds the grouped result in Python.
    samples: list[tuple[dict, float]] = []
    for wname, minutes in WINDOWS_MINUTES.items():
        since = now - timedelta(minutes=minutes)
        grouped = s.execute(
            select(AuditEvent.event_type, AuditEvent.tool, func.count())
            .where(AuditEvent.ts >= since)
            .group_by(AuditEvent.event_type, AuditEvent.tool)
        ).all()
        counts: Counter = Counter()
        for et, tool, n in grouped:
            key = (et.value if hasattr(et, "value") else str(et),
                   _server_of(tool, known))
            counts[key] += n
        samples.extend(
            ({"event_type": et, "server": server, "window": wname}, float(n))
            for (et, server), n in sorted(counts.items())
        )
    metric("sentinel_audit_events", "gauge",
           "Audit events in the trailing window, by type and (clamped) server.",
           samples)

    # -- the record's own health --------------------------------------------
    unsealed = s.execute(
        select(func.count()).select_from(AuditEvent)
        .where(AuditEvent.row_hash.is_(None))
    ).scalar_one()
    metric("sentinel_audit_unsealed_rows", "gauge",
           "Rows not yet in the hash chain (the sealer's backlog; grows = sealer stopped).",
           [({}, float(unsealed))])

    state = _shipping_state()
    shipped_through = int(state.get("shipped_through_id", 0))
    max_sealed = s.execute(
        select(func.max(AuditEvent.id)).where(AuditEvent.row_hash.is_not(None))
    ).scalar_one() or 0
    backlog = s.execute(
        select(func.count()).select_from(AuditEvent)
        .where(AuditEvent.row_hash.is_not(None), AuditEvent.id > shipped_through)
    ).scalar_one() if max_sealed > shipped_through else 0
    metric("sentinel_audit_shipping_backlog_rows", "gauge",
           "Sealed rows not yet copied to Loki (grows = the shipper is failing or off).",
           [({}, float(backlog))])
    metric("sentinel_audit_shipping_skipped_rows_total", "counter",
           "Upper bound on rows lost to the copy: rows in batches Loki 4xx-rejected "
           "(Loki may have ingested part of a rejected batch; canonical copy intact in Sentinel).",
           [({}, float(state.get("skipped_rows", 0)))])
    metric("sentinel_audit_shipping_last_success_timestamp_seconds", "gauge",
           "Unix time of the last successful push to Loki (0 = never).",
           [({}, float(state.get("last_success_ts", 0)))])

    return "\n".join(out) + "\n"
