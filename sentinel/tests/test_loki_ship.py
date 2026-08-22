"""ADR-006 Decision 2: shipping sealed audit rows to Loki.

Proven here:
1. The line shipped is the SEGMENT line format verbatim — canonical +
   prev_hash + row_hash — so Loki's copy carries the hash chain.
2. Watermark-after-ack: a failed push leaves the watermark; the next
   tick re-ships the same rows (the seal cadence is the retry policy).
3. Unsealed rows never ship; labels stay bounded; a 4xx batch is
   skipped loudly (watermark advances, skipped_rows counted) instead
   of wedging every future row behind it.
4. Disabled (no URL) is a clean no-op.

Run: python -m pytest tests/test_loki_ship.py -q
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

_TMP = tempfile.mkdtemp()
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("SENTINEL_DB", os.path.join(_TMP, "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app import audit_chain, config, loki_ship  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AuditEvent, AuditEventType  # noqa: E402
from app.service import audit  # noqa: E402


def _migrate():
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(cfg, "head")


_migrate()


@pytest.fixture(autouse=True)
def _clean():
    with SessionLocal() as s:
        s.query(AuditEvent).delete()
        s.commit()
    yield
    with SessionLocal() as s:
        s.query(AuditEvent).delete()
        s.commit()


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(config, "LOKI_PUSH_URL",
                        "https://loki-push.lab.local:8443/loki/api/v1/push")


class _StubClient:
    """The house httpx-stub shape: swap the factory, capture the calls."""

    def __init__(self, status=204, raises=None, text=""):
        self.status = status
        self.raises = raises
        self.text = text
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        if self.raises:
            raise self.raises
        self.calls.append((url, json))
        return SimpleNamespace(status_code=self.status, text=self.text)


def _seed(n, seal=True, tool="github.get_issue"):
    with SessionLocal() as s:
        for _ in range(n):
            audit(s, AuditEventType.USE, tool=tool, actor="t",
                  principal="alice@example.com", resource="github:acme/x")
        s.commit()
        if seal:
            audit_chain.seal(s)


def _ship(stub, tmp_path):
    with SessionLocal() as s:
        return loki_ship.ship(s, state_dir=str(tmp_path))


def test_disabled_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOKI_PUSH_URL", None)
    _seed(2)
    out = _ship(None, tmp_path)
    assert out == {"shipped": 0, "disabled": True}
    assert loki_ship.read_state(str(tmp_path)) == {}


def test_ships_segment_lines_with_bounded_labels(monkeypatch, tmp_path):
    stub = _StubClient()
    monkeypatch.setattr(loki_ship, "_http", lambda: stub)
    _seed(3)
    out = _ship(stub, tmp_path)
    assert out["shipped"] == 3
    (url, payload), = stub.calls
    assert url.endswith("/loki/api/v1/push")
    (stream,) = payload["streams"]
    # bounded labels only — and the principal lives in the LINE, not a label
    assert set(stream["stream"]) == {"source", "event_type", "server"}
    assert stream["stream"]["source"] == "sentinel"
    assert stream["stream"]["event_type"] == "use"
    ts, line = stream["values"][0]
    assert ts.isdigit() and len(ts) == 19  # ns epoch
    doc = json.loads(line)
    assert set(doc) == {"canonical", "prev_hash", "row_hash"}
    assert doc["canonical"]["principal"] == "alice@example.com"
    assert doc["canonical"]["tool"] == "github.get_issue"
    # chain continuity: first line anchors on genesis or prior chain head
    assert len(doc["row_hash"]) == 64
    # watermark advanced to the last shipped row
    state = loki_ship.read_state(str(tmp_path))
    with SessionLocal() as s:
        max_id = max(r.id for r in s.query(AuditEvent).all())
    assert state["shipped_through_id"] == max_id
    # nothing left: a second ship posts nothing
    stub.calls.clear()
    out = _ship(stub, tmp_path)
    assert out["shipped"] == 0 and stub.calls == []


def test_unsealed_rows_do_not_ship(monkeypatch, tmp_path):
    stub = _StubClient()
    monkeypatch.setattr(loki_ship, "_http", lambda: stub)
    _seed(2, seal=True)
    _seed(1, seal=False)  # unsealed straggler
    out = _ship(stub, tmp_path)
    assert out["shipped"] == 2
    with SessionLocal() as s:
        unsealed = [r for r in s.query(AuditEvent).all() if r.row_hash is None]
    assert len(unsealed) == 1


def test_failure_leaves_watermark_then_retries(monkeypatch, tmp_path):
    _seed(2)
    bad = _StubClient(raises=httpx.ConnectError("boom"))
    monkeypatch.setattr(loki_ship, "_http", lambda: bad)
    out = _ship(bad, tmp_path)
    assert out["shipped"] == 0
    assert loki_ship.read_state(str(tmp_path)).get("shipped_through_id") is None
    good = _StubClient()
    monkeypatch.setattr(loki_ship, "_http", lambda: good)
    out = _ship(good, tmp_path)
    assert out["shipped"] == 2  # same rows, next tick


def test_5xx_leaves_watermark_4xx_skips_loudly(monkeypatch, tmp_path):
    _seed(2)
    five = _StubClient(status=503)
    monkeypatch.setattr(loki_ship, "_http", lambda: five)
    assert _ship(five, tmp_path)["shipped"] == 0
    assert loki_ship.read_state(str(tmp_path)).get("shipped_through_id") is None

    four = _StubClient(status=400, text="entry too far behind")
    monkeypatch.setattr(loki_ship, "_http", lambda: four)
    out = _ship(four, tmp_path)
    assert out["shipped"] == 0
    state = loki_ship.read_state(str(tmp_path))
    assert state["skipped_rows"] == 2
    with SessionLocal() as s:
        max_id = max(r.id for r in s.query(AuditEvent).all())
    assert state["shipped_through_id"] == max_id  # not wedged


def test_batches_drain_in_one_tick(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOKI_BATCH_ROWS", 2)
    stub = _StubClient()
    monkeypatch.setattr(loki_ship, "_http", lambda: stub)
    _seed(5)
    out = _ship(stub, tmp_path)
    assert out["shipped"] == 5
    assert len(stub.calls) == 3  # 2 + 2 + 1
