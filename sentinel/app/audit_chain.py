"""7.6 — making the record durable: tamper evidence, retention, export.

The audit log has been the canonical account of who did what since
5.5, and until now it was a plain mutable table. Anyone with the
database file could edit or delete a row and nothing would notice —
which is a poor property for the one artifact that answers "what
happened". Three parts, in the order they matter:

**Tamper evidence (a hash chain).** Each sealed row carries
`prev_hash` and `row_hash`, where `row_hash = sha256(prev_hash ||
canonical(row))`. Change or remove any sealed row and every later hash
stops matching, so alteration is detectable even though it is not
preventable — nothing stored on a machine can be made unalterable by
its own administrator, and claiming otherwise would be the wrong kind
of confidence.

**Sealing happens in a PASS, not on insert.** `audit()` is on every
hot path in three separate processes; making each write first read its
predecessor would turn the record into a lock contention point, and
two processes reading the same predecessor would fork the chain. So
rows land unsealed and a single sealer chains them in id order. An
unsealed row is still the truth — it is simply not yet provable.

**Retention that does not break the chain.** Deleting old rows from a
hash chain destroys it. Rotation instead SEALS a segment, writes it
out as JSONL with its terminal hash, and only then removes those rows,
recording the segment so verification can still span the gap. The
JSONL is also the export: Loki, a SIEM, or anything else that reads a
line at a time (Phase 8's dashboards are the local consumer).
"""

import hashlib
import json
import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent, utcnow

log = logging.getLogger("sentinel")

GENESIS = "0" * 64


def canonical(row: AuditEvent) -> str:
    """The bytes a row's hash covers. Explicit field list, sorted keys,
    no whitespace: a canonical form that changes when a value changes
    and NOT when SQLAlchemy, Python, or a JSON library changes its
    mind about ordering. Adding a field later means old rows verify
    against the old form — so any future field must be appended and
    the version bumped, never inserted."""
    return json.dumps({
        "v": 1,
        "id": row.id,
        "ts": row.ts.isoformat() if row.ts else None,
        "event_type": row.event_type.value if row.event_type else None,
        "flow_id": row.flow_id,
        "tool": row.tool,
        "actor": row.actor,
        "principal": row.principal,
        "resource": row.resource,
        "policy_version": row.policy_version,
        "details": row.details,
    }, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev: str, row: AuditEvent) -> str:
    return hashlib.sha256((prev + canonical(row)).encode()).hexdigest()


def seal(s: Session, limit: int = 5000) -> int:
    """Chain any unsealed rows, oldest first. Returns how many were
    sealed. Safe to call repeatedly and from a single process; running
    two sealers concurrently is the one thing that would fork the
    chain, so exactly one process owns this (the admin console)."""
    last = s.scalars(
        select(AuditEvent).where(AuditEvent.row_hash.is_not(None))
        .order_by(AuditEvent.id.desc()).limit(1)
    ).first()
    prev = last.row_hash if last else GENESIS

    pending = s.scalars(
        select(AuditEvent).where(AuditEvent.row_hash.is_(None))
        .order_by(AuditEvent.id.asc()).limit(limit)
    ).all()
    for row in pending:
        row.prev_hash = prev
        row.row_hash = prev = _hash(prev, row)
    if pending:
        s.commit()
    return len(pending)


def verify(s: Session, anchor: str = GENESIS) -> dict:
    """Recompute the chain over sealed rows. Returns a verdict naming
    the FIRST row that does not match — the whole point is to say
    where the record stopped being trustworthy, not merely that it
    did. `anchor` is the terminal hash of the last exported segment
    when older rows have been rotated out."""
    rows = s.scalars(
        select(AuditEvent).where(AuditEvent.row_hash.is_not(None))
        .order_by(AuditEvent.id.asc())
    ).all()
    prev = anchor
    for row in rows:
        if row.prev_hash != prev:
            return {"ok": False, "checked": rows.index(row),
                    "broken_at_id": row.id,
                    "detail": "row does not follow its predecessor — a row "
                              "was removed, reordered, or inserted"}
        if _hash(prev, row) != row.row_hash:
            return {"ok": False, "checked": rows.index(row),
                    "broken_at_id": row.id,
                    "detail": "row content does not match its hash — the row "
                              "was edited after it was written"}
        prev = row.row_hash
    unsealed = s.scalar(
        select(AuditEvent).where(AuditEvent.row_hash.is_(None)).limit(1))
    return {"ok": True, "checked": len(rows), "head": prev,
            "unsealed_present": unsealed is not None}


def export_and_prune(s: Session, out_dir: str, retain_days: int,
                     dry_run: bool = False) -> dict:
    """Write rows older than `retain_days` to a JSONL segment, then
    remove them — in that order, and only for rows that are SEALED, so
    nothing leaves the database without its hash and nothing is deleted
    that was not first written somewhere else.

    The segment records its own first/last id and terminal hash, which
    is what lets `verify` continue across the gap instead of treating
    a rotation as tampering."""
    cutoff = utcnow() - timedelta(days=retain_days)
    old = s.scalars(
        select(AuditEvent)
        .where(AuditEvent.ts < cutoff, AuditEvent.row_hash.is_not(None))
        .order_by(AuditEvent.id.asc())
    ).all()
    if not old:
        return {"exported": 0, "pruned": 0, "segment": None}

    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = f"audit-{old[0].id:012d}-{old[-1].id:012d}.jsonl"
    path = d / name
    if dry_run:
        return {"exported": len(old), "pruned": 0, "segment": str(path),
                "dry_run": True}

    # Write first, fsync, THEN delete. A crash between the two leaves a
    # duplicate segment, which is recoverable; the other order loses
    # the record, which is not.
    import os
    with open(path, "w") as f:
        for row in old:
            f.write(json.dumps({
                "canonical": json.loads(canonical(row)),
                "prev_hash": row.prev_hash, "row_hash": row.row_hash,
            }, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    (d / "segments.json").write_text(json.dumps(_index(d, old, name), indent=1))

    for row in old:
        s.delete(row)
    s.commit()
    log.info("audit: exported %d rows to %s and pruned them", len(old), path)
    return {"exported": len(old), "pruned": len(old), "segment": str(path),
            "terminal_hash": old[-1].row_hash}


def _index(d: Path, rows, name: str) -> list:
    idx = []
    p = d / "segments.json"
    if p.exists():
        try:
            idx = json.loads(p.read_text())
        except ValueError:
            idx = []
    idx.append({"file": name, "first_id": rows[0].id, "last_id": rows[-1].id,
                "first_ts": rows[0].ts.isoformat(),
                "last_ts": rows[-1].ts.isoformat(),
                "terminal_hash": rows[-1].row_hash})
    return idx


def anchor_from_segments(out_dir: str) -> str:
    """The terminal hash of the most recent exported segment, so
    verification of the live table continues the chain rather than
    starting over."""
    p = Path(out_dir) / "segments.json"
    if not p.exists():
        return GENESIS
    try:
        idx = json.loads(p.read_text())
        return idx[-1]["terminal_hash"] if idx else GENESIS
    except (ValueError, KeyError, IndexError):
        return GENESIS
