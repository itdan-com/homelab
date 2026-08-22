"""ADR-006 Decision 1: the broker's /metrics.

What is proven here, in order of importance:
1. THE LABEL DISCIPLINE — no principal, no resource, ever. The test
   plants an email and a repo path in audit rows and asserts neither
   string appears anywhere in the rendered exposition.
2. Server clamping — a tool whose server is not in the active policy
   store renders as "other", so hostile tool names cannot mint labels.
3. The state gauges (kill switch, live grants by door, policy info,
   unsealed rows, shipping backlog) read what the DB actually says.

Run: python -m pytest tests/test_metrics.py -q
"""
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP = tempfile.mkdtemp()
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("SENTINEL_DB", os.path.join(_TMP, "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app import audit_chain, broker, loki_ship  # noqa: E402
from app import metrics as metrics_mod  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuditEvent,
    AuditEventType,
    CapabilityGrant,
    utcnow,
)
from app.service import audit  # noqa: E402


def _migrate():
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(cfg, "head")


_migrate()
client = TestClient(broker.app)


@pytest.fixture(autouse=True)
def _clean():
    with SessionLocal() as s:
        s.query(AuditEvent).delete()
        s.commit()
    yield
    with SessionLocal() as s:
        s.query(AuditEvent).delete()
        s.commit()


@pytest.fixture()
def _policy(monkeypatch):
    """An active policy whose server list is exactly {github}."""
    ap = SimpleNamespace(servers={"github": {}}, version="test-version-1")
    monkeypatch.setattr(metrics_mod.policy, "get_active", lambda: ap)
    return ap


def _live_counts(body: str) -> dict:
    """Parse the sentinel_grants_live samples out of an exposition."""
    out = {}
    for line in body.splitlines():
        if line.startswith("sentinel_grants_live{"):
            via = line.split('granted_via="')[1].split('"')[0]
            out[via] = float(line.rsplit(" ", 1)[1])
    return out


@pytest.fixture()
def _grants():
    """Three grants: live-confirm, expired-admin, revoked-approve.
    Deleted by id afterwards — a blanket grant DELETE breaks other
    files' still-live rows (the 2026-08-15 cross-file lesson). For the
    same reason the test asserts DELTAS against the pre-existing counts:
    other files legitimately leave live grants in the shared DB."""
    before = _live_counts(_scrape())
    now = utcnow()
    ids = []
    with SessionLocal() as s:
        for suffix, via, expires, revoked in (
            ("live", "confirm", now + timedelta(minutes=30), None),
            ("expired", "admin", now - timedelta(minutes=1), None),
            ("revoked", "approve", now + timedelta(minutes=30), now),
        ):
            g = CapabilityGrant(
                tool="github.create_pull_request",
                granted_via=via,
                token_hash=f"metrics-test-{suffix}" + "0" * 40,
                expires_at=expires,
                granted_by="test",
                revoked_at=revoked,
            )
            s.add(g)
            s.flush()
            ids.append(g.id)
        s.commit()
    yield before, ids
    with SessionLocal() as s:
        s.query(CapabilityGrant).filter(CapabilityGrant.id.in_(ids)).delete()
        s.commit()


def _scrape() -> str:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    return r.text


def test_bounded_labels_never_leak_principal_or_resource(_policy):
    with SessionLocal() as s:
        audit(s, AuditEventType.DENIAL, tool="github.create_pull_request",
              actor="ladder", principal="alice@example.com",
              resource="github:acme/payroll", details={"outcome": "forbid"})
        s.commit()
    body = _scrape()
    assert "alice@example.com" not in body
    assert "acme/payroll" not in body
    assert 'event_type="denial"' in body
    assert 'server="github"' in body


def test_server_clamps_to_policy_set(_policy):
    with SessionLocal() as s:
        audit(s, AuditEventType.USE, tool="evil-server.exfiltrate", actor="t")
        audit(s, AuditEventType.USE, tool="github.get_issue", actor="t")
        audit(s, AuditEventType.KILL_ENGAGED, tool=None, actor="console")
        s.commit()
    body = _scrape()
    assert "evil-server" not in body                       # hostile text never a label
    assert 'event_type="use",server="other"' in body
    assert 'event_type="use",server="github"' in body
    assert 'event_type="kill_engaged",server=""' in body   # tool-less events


def test_state_gauges(_policy, _grants):
    before, _fixture = _grants
    body = _scrape()
    # tolerant of shared-DB kill state left by other files; must render
    # a bare 0-or-1 either way
    assert ("sentinel_kill_switch_engaged 0" in body
            or "sentinel_kill_switch_engaged 1" in body)
    assert 'sentinel_policy_info{version="test-version-1"} 1' in body
    assert "sentinel_policy_active 1" in body
    # of the three fixture grants only live-confirm counts, and only
    # under its own door — measured as a delta against whatever live
    # grants other test files left in the shared DB
    after = _live_counts(body)
    assert after.get("confirm", 0) - before.get("confirm", 0) == 1
    assert after.get("admin", 0) - before.get("admin", 0) == 0
    assert after.get("approve", 0) - before.get("approve", 0) == 0
    assert "sentinel_requests_pending " in body  # gauge renders (value is shared-DB state)


def test_no_active_policy_still_renders(monkeypatch):
    monkeypatch.setattr(metrics_mod.policy, "get_active", lambda: None)
    with SessionLocal() as s:
        audit(s, AuditEventType.USE, tool="github.get_issue", actor="t")
        s.commit()
    body = _scrape()
    assert "sentinel_policy_active 0" in body
    assert "sentinel_policy_info" not in body
    # without a store there is no bounded server set: everything clamps
    assert 'event_type="use",server="other"' in body


def test_seal_and_shipping_gauges(_policy, monkeypatch, tmp_path):
    monkeypatch.setattr(metrics_mod, "AUDIT_EXPORT_DIR", str(tmp_path))
    with SessionLocal() as s:
        audit(s, AuditEventType.USE, tool="github.get_issue", actor="t")
        audit(s, AuditEventType.USE, tool="github.get_issue", actor="t")
        s.commit()
    body = _scrape()
    assert "sentinel_audit_unsealed_rows 2" in body

    with SessionLocal() as s:
        audit_chain.seal(s)
        max_id = max(r.id for r in s.query(AuditEvent).all())
    body = _scrape()
    assert "sentinel_audit_unsealed_rows 0" in body
    assert "sentinel_audit_shipping_backlog_rows 2" in body  # sealed, never shipped

    # 1755859312 is the review's proof value: %g renders it 1.75586e+09
    # (688 seconds wrong) — full precision or bust
    loki_ship._write_state(
        {"shipped_through_id": max_id, "last_success_ts": 1755859312,
         "skipped_rows": 3},
        state_dir=str(tmp_path))
    body = _scrape()
    assert "sentinel_audit_shipping_backlog_rows 0" in body
    assert "sentinel_audit_shipping_skipped_rows_total 3" in body
    assert "sentinel_audit_shipping_last_success_timestamp_seconds 1755859312" in body


def test_unknown_door_accumulates_not_clobbers(_policy):
    """An out-of-enum granted_via folds into `other` without touching a
    real door's count (review-caught: assignment would CLOBBER admin)."""
    now = utcnow()
    ids = []
    with SessionLocal() as s:
        before = _live_counts(_scrape())
        for suffix, via in (("adm1", "admin"), ("adm2", "admin"),
                            ("mys1", "legacy-door"), ("mys2", "future-door")):
            g = CapabilityGrant(
                tool="github.get_issue", granted_via=via,
                token_hash=f"metrics-unk-{suffix}" + "0" * 40,
                expires_at=now + timedelta(minutes=30), granted_by="test")
            s.add(g)
            s.flush()
            ids.append(g.id)
        s.commit()
    try:
        after = _live_counts(_scrape())
        assert after.get("admin", 0) - before.get("admin", 0) == 2
        assert after.get("other", 0) - before.get("other", 0) == 2
    finally:
        with SessionLocal() as s:
            s.query(CapabilityGrant).filter(CapabilityGrant.id.in_(ids)).delete()
            s.commit()


def test_lapsed_pending_request_not_counted(_policy):
    """Request expiry is lazy — the gauge must filter on expires_at or an
    abandoned request paints the dashboard red forever (review-caught)."""
    from app.models import CapabilityRequest, Flow, RequestStatus
    now = utcnow()
    with SessionLocal() as s:
        before_body = _scrape()
        before = int([l for l in before_body.splitlines()
                      if l.startswith("sentinel_requests_pending ")][0].rsplit(" ", 1)[1])
        f = Flow(id="metrics-test-flow-lapsed", agent="metrics-test")
        s.merge(f)
        s.add(CapabilityRequest(
            flow_id="metrics-test-flow-lapsed", tool="github.get_issue",
            reason="t", status=RequestStatus.PENDING,
            expires_at=now - timedelta(minutes=1)))   # lapsed, never re-read
        s.add(CapabilityRequest(
            flow_id="metrics-test-flow-lapsed", tool="github.get_issue",
            reason="t", status=RequestStatus.PENDING,
            expires_at=now + timedelta(minutes=10)))  # genuinely waiting
        s.commit()
    try:
        body = _scrape()
        after = int([l for l in body.splitlines()
                     if l.startswith("sentinel_requests_pending ")][0].rsplit(" ", 1)[1])
        assert after - before == 1
    finally:
        with SessionLocal() as s:
            s.query(CapabilityRequest).filter(
                CapabilityRequest.flow_id == "metrics-test-flow-lapsed").delete()
            s.commit()
