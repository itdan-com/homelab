"""ADR-006 Decision 2: ship sealed audit rows to Loki — on SEAL, not rotation.

Why a copy at all (the ADR's settling argument, kept where the code is):
the canonical record is one SQLite file on one host, and the 7.6 hash
chain makes tampering DETECTABLE — as far as a single copy can go. A
copy in a system with a different administrator makes erasing history
require compromising both. The line shipped here is byte-identical to
the sealed segment line (canonical + prev_hash + row_hash), so Loki's
copy carries the chain, not a paraphrase of it.

Shape: the admin process's seal loop calls ship_pending() after each
seal pass (the sealer is single-process by design, so the shipper is
too — one watermark, no races). The watermark is a small JSON sidecar
next to the segments (segments.json precedent), advanced only after
Loki acknowledges a batch. Failures leave the watermark, and the next
30-second tick retries — the seal cadence IS the retry policy, no new
machinery. A batch Loki PERMANENTLY rejects (4xx: e.g. older than its
reject window) is skipped loudly and counted, because wedging every
future row behind one poison batch inverts the priority: Sentinel is
canonical, Loki is the copy.

Labels stay bounded (ADR-006): source="sentinel", event_type, server —
principal/resource/policy_version ride IN the line where Loki's search
finds them without exploding the index. Server is clamped exactly as
/metrics clamps it.

The wire is the cluster's mTLS push route: server side verified against
the LAB CA (Traefik's cert-manager certificate), client side proving
ourselves with a Sentinel-CA leaf (loki-client) — same "hold our CA's
cert or go away" pattern as the proxy-client certificate.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import time
from datetime import timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit_chain, config
from .db import SessionLocal
from .models import AuditEvent

log = logging.getLogger("sentinel")

STATE_FILE = "loki-shipper.json"


def enabled() -> bool:
    return bool(config.LOKI_PUSH_URL)


def _http() -> httpx.Client:
    """The client cert rides an EXPLICIT ssl.SSLContext, never httpx's
    `cert=` parameter: httpx 0.28.1 silently drops `cert=` when
    `verify` is a CA-bundle path, so the handshake presented nothing
    and Traefik's gate refused every push with certificate_required —
    found live 2026-08-23 after every probe (curl-based, installer
    included) had passed, because curl honors the same pair of inputs.
    tests/test_loki_ship.py's real-mTLS regression test pins this."""
    verify: ssl.SSLContext | str | bool = config.LOKI_CA_BUNDLE or True
    if config.LOKI_CLIENT_CERT and config.LOKI_CLIENT_KEY:
        ctx = ssl.create_default_context(
            cafile=config.LOKI_CA_BUNDLE or None)
        ctx.load_cert_chain(config.LOKI_CLIENT_CERT, config.LOKI_CLIENT_KEY)
        verify = ctx
    return httpx.Client(timeout=10.0, follow_redirects=False, verify=verify)


def _state_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or config.AUDIT_EXPORT_DIR, STATE_FILE)


def read_state(state_dir: str | None = None) -> dict:
    try:
        with open(_state_path(state_dir)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(state: dict, state_dir: str | None = None) -> None:
    path = _state_path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # fsync the DIRECTORY too, or a power loss can regress the watermark
    # past a completed rename. A regressed watermark only re-ships rows
    # — Loki drops exact duplicates — with one caveat documented at
    # _payload(): a policy-store edit between failure and retry can
    # re-label the same rows and land real duplicates in the copy.
    dfd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _ns(ts) -> str:
    """Row timestamp (naive UTC, models.utcnow) -> Loki's ns-epoch string."""
    aware = ts.replace(tzinfo=timezone.utc)
    return str(int(aware.timestamp()) * 10**9 + aware.microsecond * 1000)


def _payload(rows) -> dict:
    """Group rows into Loki streams by bounded labels, lines verbatim.

    Known caveat (accepted): `server` is derived from the policy store
    AT SHIP TIME, so a store edit between a failed push and its retry
    can re-label the same rows into a different stream — Loki's
    exact-duplicate drop then misses them and the copy gains duplicate
    lines. Sentinel stays canonical either way; the alternative
    (persisting derived labels at seal time) is schema for an edge."""
    ap_servers: set[str]
    from . import policy  # local import: policy may be inactive in tests
    ap = policy.get_active()
    ap_servers = set(ap.servers) if ap else set()

    streams: dict[tuple[str, str], list[list[str]]] = {}
    for row in rows:
        et = row.event_type.value if hasattr(row.event_type, "value") else str(row.event_type)
        tool = row.tool or ""
        server = tool.partition(".")[0] if tool else ""
        if server and server not in ap_servers:
            server = "other"
        line = json.dumps(
            {"canonical": json.loads(audit_chain.canonical(row)),
             "prev_hash": row.prev_hash, "row_hash": row.row_hash},
            separators=(",", ":"))
        streams.setdefault((et, server), []).append([_ns(row.ts), line])
    return {"streams": [
        {"stream": {"source": "sentinel", "event_type": et, "server": server},
         "values": values}
        for (et, server), values in sorted(streams.items())
    ]}


def ship(s: Session, state_dir: str | None = None) -> dict:
    """Push sealed rows beyond the watermark, in batches, watermark-after-ack."""
    if not enabled():
        return {"shipped": 0, "disabled": True}
    state = read_state(state_dir)
    shipped_total = 0
    # Client construction can itself raise (missing cert file, bad CA
    # path, malformed URL) — those must land on the same calm one-line
    # retry path as a network failure, not traceback-spam the seal loop
    # every 30 seconds (review-caught: the stale-cert-set re-install is
    # exactly this shape).
    try:
        client_cm = _http()
    except (OSError, ValueError, httpx.HTTPError) as e:
        log.warning("loki shipper misconfigured (%s) — check SENTINEL_LOKI_* paths", e)
        return {"shipped": 0, "error": str(e)}
    with client_cm as client:
        for _ in range(config.LOKI_MAX_BATCHES_PER_TICK):
            watermark = int(state.get("shipped_through_id", 0))
            rows = s.execute(
                select(AuditEvent)
                .where(AuditEvent.row_hash.is_not(None), AuditEvent.id > watermark)
                .order_by(AuditEvent.id.asc())
                .limit(config.LOKI_BATCH_ROWS)
            ).scalars().all()
            if not rows:
                break
            try:
                resp = client.post(config.LOKI_PUSH_URL, json=_payload(rows))
            except (httpx.HTTPError, OSError, ValueError) as e:
                log.warning("loki push failed (will retry next seal tick): %s", e)
                break
            if resp.status_code in (200, 204):
                state["shipped_through_id"] = rows[-1].id
                state["last_success_ts"] = int(time.time())
                _write_state(state, state_dir)
                shipped_total += len(rows)
                continue
            if 400 <= resp.status_code < 500:
                # Permanent rejection: skip the batch LOUDLY rather than
                # wedge every future row behind it. Canonical copy intact.
                log.error("loki rejected batch %s-%s (%s): %s — skipping",
                          rows[0].id, rows[-1].id, resp.status_code,
                          resp.text[:200])
                state["shipped_through_id"] = rows[-1].id
                state["skipped_rows"] = int(state.get("skipped_rows", 0)) + len(rows)
                _write_state(state, state_dir)
                continue
            log.warning("loki push %s (will retry next seal tick)", resp.status_code)
            break
    return {"shipped": shipped_total,
            "shipped_through_id": int(state.get("shipped_through_id", 0)),
            "skipped_rows": int(state.get("skipped_rows", 0))}


def ship_pending() -> dict:
    """Entry point for the seal loop: own session, thread-safe to call
    via asyncio.to_thread (httpx blocks; the console must not)."""
    with SessionLocal() as s:
        return ship(s)
