"""7.2.4 — the Access screen's API: save-and-activate over HTTP, the
rejected-save-touches-nothing property, history, restore-forward, the
policy_change audit trail, and the console guard on the new routes.

    python -m pytest tests/test_policy_endpoints.py -q
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import select  # noqa: E402

_HERE = os.path.dirname(__file__)


def _migrate() -> None:
    cfg = Config(os.path.join(_HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_HERE, "..", "migrations"))
    command.upgrade(cfg, "head")


_migrate()

from starlette.testclient import TestClient  # noqa: E402

from app import policy  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as admin_app  # noqa: E402
from app.models import AuditEvent, AuditEventType  # noqa: E402
from authkit import sign_in  # noqa: E402

_EXAMPLE = Path(__file__).resolve().parents[1] / "policy-example"
CONSOLE = {"x-sentinel-console": "1"}


def _docs() -> dict:
    return {key: (_EXAMPLE / name).read_text()
            for key, name in policy._DOCS.items()}


@pytest.fixture()
def store_dir(tmp_path, monkeypatch):
    d = tmp_path / "store"
    d.mkdir()
    monkeypatch.setattr(policy, "POLICY_DIR", str(d))
    monkeypatch.setattr(policy, "_active", None)  # no bleed between files
    return d


@pytest.fixture()
def admin():
    c = TestClient(admin_app, base_url="https://testserver")
    sign_in(c, username=f"acc-{uuid.uuid4().hex[:6]}", label="access-key")
    return c


def test_store_lifecycle(store_dir, admin):
    # Fresh dir: nothing active, editors empty but editable.
    r = admin.get("/v1/policy/store", headers=CONSOLE)
    assert r.status_code == 200 and r.json()["active"] is False

    # Save the example store → activated.
    good = _docs()
    r = admin.put("/v1/policy/store", json=good, headers=CONSOLE)
    assert r.status_code == 200
    v1 = r.json()["version"]

    r = admin.get("/v1/policy/store", headers=CONSOLE).json()
    assert r["active"] and r["version"] == v1
    assert "engineering" in r["matrix"]["grants"]
    assert r["documents"]["matrix"] == good["matrix"]

    # A broken save is rejected AND leaves the disk untouched.
    broken = dict(good, matrix="grants:\n  engineering:\n    github:\n      level: sudo\n")
    r = admin.put("/v1/policy/store", json=broken, headers=CONSOLE)
    assert r.status_code == 422
    assert any("bad level" in e for e in r.json()["detail"])
    r = admin.get("/v1/policy/store", headers=CONSOLE).json()
    assert r["version"] == v1                       # last-good serving
    assert r["documents"]["matrix"] == good["matrix"]  # disk untouched

    # A real change makes a new version; history shows both.
    edited = dict(good, matrix=good["matrix"].replace(
        "level: write-on-request", "level: write"))
    v2 = admin.put("/v1/policy/store", json=edited,
                   headers=CONSOLE).json()["version"]
    assert v2 != v1
    hist = admin.get("/v1/policy/history", headers=CONSOLE).json()
    assert [h["version"] for h in hist[:2]] == [v2, v1]
    assert hist[0]["current"] is True

    # Restore v1: forward, content-hash brings the SAME version id back.
    r = admin.post("/v1/policy/revert", json={"version": v1}, headers=CONSOLE)
    assert r.status_code == 200 and r.json()["version"] == v1
    r = admin.get("/v1/policy/store", headers=CONSOLE).json()
    assert r["version"] == v1
    assert r["documents"]["matrix"] == good["matrix"]
    hist = admin.get("/v1/policy/history", headers=CONSOLE).json()
    assert hist[0]["version"] == v1 and hist[0]["current"] is True
    assert len(hist) == 3                            # nothing rewritten

    # The trail: activations and the rejection are all policy_change rows.
    with SessionLocal() as s:
        rows = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.POLICY_CHANGE,
        )).all()
        results = [(r.details or {}).get("result") for r in rows]
        assert results.count("activated") >= 3
        assert results.count("rejected") >= 1


def test_structured_save_roundtrip(store_dir, admin):
    """The GUI's path (7.2.6): parsed objects in, same gate, YAML out —
    overlay preserved from disk, deterministic version on unchanged
    intent, rejection leaves everything standing."""
    admin.put("/v1/policy/store", json=_docs(), headers=CONSOLE)  # seed raw
    store = admin.get("/v1/policy/store", headers=CONSOLE).json()
    body = {"groups": store["groups"], "people": store["people"],
            "matrix": store["matrix"], "servers": store["servers_detail"]}
    # A GUI-shaped edit: one new person, one matrix cell flipped.
    body["people"]["newbie@example.com"] = {"groups": ["engineering"]}
    body["matrix"]["grants"]["engineering"]["echo"] = {"level": "read"}

    r = admin.put("/v1/policy/store/structured", json=body, headers=CONSOLE)
    assert r.status_code == 200
    v1 = r.json()["version"]

    after = admin.get("/v1/policy/store", headers=CONSOLE).json()
    assert "newbie@example.com" in after["people"]
    assert after["matrix"]["grants"]["engineering"]["echo"]["level"] == "read"
    # The escape hatch survives a GUI save untouched.
    assert after["documents"]["overlay"] == _docs()["overlay"]

    # Same intent again → same version (deterministic emission).
    r = admin.put("/v1/policy/store/structured", json=body, headers=CONSOLE)
    assert r.status_code == 200 and r.json()["version"] == v1

    # Garbage shapes fail the SAME gate, with the store left standing.
    bad = dict(body, matrix={"grants": {"ghosts": {"echo": {"level": "read"}}}})
    r = admin.put("/v1/policy/store/structured", json=bad, headers=CONSOLE)
    assert r.status_code == 422
    assert any("unknown group" in e for e in r.json()["detail"])
    still = admin.get("/v1/policy/store", headers=CONSOLE).json()
    assert still["version"] == v1
    assert "newbie@example.com" in still["people"]


def test_unknown_version_restore_is_422(store_dir, admin):
    admin.put("/v1/policy/store", json=_docs(), headers=CONSOLE)
    r = admin.post("/v1/policy/revert", json={"version": "0" * 12},
                   headers=CONSOLE)
    assert r.status_code == 422


def test_save_requires_console_guard(store_dir, admin):
    r = admin.put("/v1/policy/store", json=_docs())  # no CSRF header
    assert r.status_code == 403


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
