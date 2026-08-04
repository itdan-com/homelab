"""7.6: the record made durable — tamper evidence, retention, export.

The interesting tests are the adversarial ones: edit a row, delete a
row, reorder the ids, and the chain must say WHERE it broke. A chain
that only reports "something is wrong" is nearly useless to whoever
has to reconstruct an incident.

    python -m pytest tests/test_audit_chain.py -q
"""

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(_TMP, "test.db"))
# This module now sorts FIRST, so it is the one that imports app.config
# — and config reads the environment once, at import. Leaving this out
# meant every console test that ran later got a 400 from the host
# allowlist. Test files that touch config must set the whole env, not
# the part they personally need.
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import audit_chain  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AuditEvent, AuditEventType, utcnow  # noqa: E402
from app.service import audit  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]


def _migrate():
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(cfg, "head")


_migrate()


@pytest.fixture(autouse=True)
def _clean():
    with SessionLocal() as s:
        for row in s.scalars(select(AuditEvent)).all():
            s.delete(row)
        s.commit()
    yield


def _write(n: int, **kw):
    with SessionLocal() as s:
        for i in range(n):
            audit(s, AuditEventType.USE, tool=f"t{i}", principal="a@b.c", **kw)
        s.commit()


# --- sealing ------------------------------------------------------------------

def test_rows_arrive_unsealed_and_a_pass_chains_them(c=None):
    """Sealing is a pass, not an insert hook: audit() is on every hot
    path in three processes, and making each write read its
    predecessor would make the record a contention point."""
    _write(3)
    with SessionLocal() as s:
        assert all(r.row_hash is None for r in s.scalars(select(AuditEvent)).all())
        assert audit_chain.seal(s) == 3
        rows = s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
        assert rows[0].prev_hash == audit_chain.GENESIS
        assert rows[1].prev_hash == rows[0].row_hash
        assert rows[2].prev_hash == rows[1].row_hash


def test_sealing_is_idempotent_and_resumes(c=None):
    _write(2)
    with SessionLocal() as s:
        audit_chain.seal(s)
        assert audit_chain.seal(s) == 0
    _write(2)
    with SessionLocal() as s:
        assert audit_chain.seal(s) == 2
        assert audit_chain.verify(s)["ok"] is True


def test_verify_is_happy_with_unsealed_rows_but_says_so(c=None):
    _write(2)
    with SessionLocal() as s:
        audit_chain.seal(s)
    _write(1)
    with SessionLocal() as s:
        out = audit_chain.verify(s)
        assert out["ok"] is True and out["unsealed_present"] is True


# --- tampering ----------------------------------------------------------------

def test_editing_a_row_is_detected_and_located(c=None):
    """The point of the chain: not 'something changed' but WHICH row,
    and that it was edited rather than removed."""
    _write(5)
    with SessionLocal() as s:
        audit_chain.seal(s)
        target = s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()[2]
        target_id = target.id
        target.tool = "something-else"      # rewrite history
        s.commit()
    with SessionLocal() as s:
        out = audit_chain.verify(s)
        assert out["ok"] is False
        assert out["broken_at_id"] == target_id
        assert "edited" in out["detail"]


def test_deleting_a_row_is_detected(c=None):
    _write(5)
    with SessionLocal() as s:
        audit_chain.seal(s)
        rows = s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
        gone, follower_id = rows[2], rows[3].id
        s.delete(gone)
        s.commit()
    with SessionLocal() as s:
        out = audit_chain.verify(s)
        assert out["ok"] is False
        assert out["broken_at_id"] == follower_id
        assert "removed" in out["detail"]


def test_a_forged_row_appended_after_the_head_is_detected(c=None):
    """An attacker who knows the scheme can append a plausible row —
    but not one whose prev_hash matches a head they cannot recompute
    without every prior row's exact bytes."""
    _write(3)
    with SessionLocal() as s:
        audit_chain.seal(s)
        s.add(AuditEvent(event_type=AuditEventType.GRANT, tool="forged",
                         prev_hash="0" * 64, row_hash="f" * 64))
        s.commit()
    with SessionLocal() as s:
        assert audit_chain.verify(s)["ok"] is False


# --- retention + export -------------------------------------------------------

def test_rotation_writes_before_it_deletes(tmp_path):
    """Order matters: a crash between the two should leave a duplicate
    segment (recoverable), never a lost record (not)."""
    _write(4)
    with SessionLocal() as s:
        audit_chain.seal(s)
        old = s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()[:2]
        for r in old:
            r.ts = utcnow() - timedelta(days=200)
        s.commit()
        out = audit_chain.export_and_prune(s, str(tmp_path), retain_days=90)
    assert out["exported"] == 2 and out["pruned"] == 2
    lines = Path(out["segment"]).read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["canonical"]["tool"] == "t0" and first["row_hash"]
    with SessionLocal() as s:
        assert len(s.scalars(select(AuditEvent)).all()) == 2


def test_a_dry_run_moves_nothing(tmp_path):
    """A button that deletes audit history on first click is a button
    that gets clicked by accident."""
    _write(2)
    with SessionLocal() as s:
        audit_chain.seal(s)
        for r in s.scalars(select(AuditEvent)).all():
            r.ts = utcnow() - timedelta(days=200)
        s.commit()
        out = audit_chain.export_and_prune(s, str(tmp_path), 90, dry_run=True)
    assert out["exported"] == 2 and out["pruned"] == 0
    with SessionLocal() as s:
        assert len(s.scalars(select(AuditEvent)).all()) == 2


def test_verification_spans_a_rotation(tmp_path):
    """Deleting rows from a hash chain destroys it — unless the
    exported segment's terminal hash becomes the anchor the remaining
    rows are verified against. Otherwise every rotation would look
    exactly like tampering."""
    _write(4)
    with SessionLocal() as s:
        audit_chain.seal(s)
        for r in s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()[:2]:
            r.ts = utcnow() - timedelta(days=200)
        s.commit()
        audit_chain.export_and_prune(s, str(tmp_path), 90)
        # without the anchor it looks broken...
        assert audit_chain.verify(s)["ok"] is False
        # ...and with it, the record is intact across the gap
        anchor = audit_chain.anchor_from_segments(str(tmp_path))
        assert audit_chain.verify(s, anchor)["ok"] is True


def test_unsealed_rows_are_never_exported(tmp_path):
    """Nothing leaves the database without its hash — otherwise the
    segment could not be verified later, which is the whole reason it
    exists."""
    _write(2)
    with SessionLocal() as s:
        for r in s.scalars(select(AuditEvent)).all():
            r.ts = utcnow() - timedelta(days=200)
        s.commit()
        out = audit_chain.export_and_prune(s, str(tmp_path), 90)
    assert out["exported"] == 0
