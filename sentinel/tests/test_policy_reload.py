"""7.3.1: the cross-process reload path — a console activation in the
ADMIN process reaches the BROKER process without a restart.

The two processes share nothing but the store directory, so these
tests simulate "the other process" the honest way: reset this
module's in-process state (_active, _watch_sig) to what a freshly
started or stale process would hold, then drive the watcher tick.

    python -m pytest tests/test_policy_reload.py -q
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import broker, policy  # noqa: E402

_EXAMPLE = Path(__file__).resolve().parents[1] / "policy-example"


def _mk_store(tmp: Path, name: str = "store") -> Path:
    d = Path(tmp) / name
    d.mkdir()
    for doc in ("entities.yaml", "matrix.yaml", "servers.yaml", "overlay.cedar"):
        (d / doc).write_text((_EXAMPLE / doc).read_text())
    return d


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    """Every test starts as a process that has loaded nothing; teardown
    restores whatever the wider suite had pinned (test_ladder shares
    the module global)."""
    monkeypatch.setattr(policy, "_active", None)
    monkeypatch.setattr(policy, "_watch_sig", None)
    yield


def test_refresh_is_read_only_and_version_matches_activate(tmp_path):
    """Same bytes → same version, with no coordination: refresh()
    computes the identical content hash activate() does, and writes
    NOTHING — no generated/, no .git — so the broker can never race
    the console on the store's artifacts, and git history stays
    authored by console actors alone."""
    d = _mk_store(tmp_path)
    ap = policy.refresh(d)
    assert not (d / "generated").exists()
    assert not (d / ".git").exists()
    assert policy.activate(d, actor="t").version == ap.version


def test_console_save_reaches_a_stale_process_in_one_tick(tmp_path):
    """The 7.2.5 gap in miniature: the console saves in one process; a
    process still serving the OLD version converges on its next
    watcher tick, without restart."""
    d = _mk_store(tmp_path)
    ap1 = policy.activate(d, actor="console")
    sig1 = policy.store_signature(d)

    docs = policy.store_documents(d)
    docs["matrix"] += "\n# console edit: comments change bytes, not meaning\n"
    policy.save_and_activate(d, docs, actor="console")
    v2 = policy.get_active().version
    assert v2 != ap1.version

    # now BE the stale broker: old policy in memory, old signature seen
    policy._active = ap1
    policy._watch_sig = sig1
    assert policy.maybe_refresh(d) == v2
    assert policy.get_active().version == v2


def test_unchanged_store_is_a_noop_tick(tmp_path):
    d = _mk_store(tmp_path)
    policy.refresh(d)
    policy._watch_sig = policy.store_signature(d)
    assert policy.maybe_refresh(d) is None


def test_broken_disk_edit_keeps_last_good_and_the_fix_is_picked_up(tmp_path):
    """A hand-edit that breaks the store must not take this process's
    policy down (last-good stays live), must log once instead of every
    tick, and the FIX must be picked up — retry is keyed on the store
    changing again, not abandoned."""
    d = _mk_store(tmp_path)
    ap1 = policy.refresh(d)
    policy._watch_sig = policy.store_signature(d)
    good = (d / "matrix.yaml").read_text()

    (d / "matrix.yaml").write_text("grants: {")  # not YAML
    assert policy.maybe_refresh(d) is None
    assert policy.get_active().version == ap1.version
    assert policy.maybe_refresh(d) is None  # same broken state: no re-log, no change
    assert policy.get_active().version == ap1.version

    (d / "matrix.yaml").write_text(good + "\n# fixed\n")
    assert policy.maybe_refresh(d) is not None
    assert policy.get_active().version != ap1.version


def test_mid_write_store_is_skipped_not_activated(tmp_path, monkeypatch):
    """A console save lands as four sequential file writes; a tick
    catching the store half-written must neither activate a phantom
    version nor mark the signature handled — the next tick retries."""
    d = _mk_store(tmp_path)
    orig = policy.store_signature
    seen = {"n": 0}

    def torn(pd):
        seen["n"] += 1
        s = orig(pd)
        # call 1 = the tick's look, call 2 = refresh's 'before',
        # call 3 = refresh's 'after' — simulate a write landing then
        return s + (("mid-write",),) if seen["n"] == 3 else s

    monkeypatch.setattr(policy, "store_signature", torn)
    assert policy.maybe_refresh(d) is None
    assert policy.get_active() is None  # nothing phantom swapped in

    monkeypatch.setattr(policy, "store_signature", orig)
    assert policy.maybe_refresh(d) is not None  # untouched _watch_sig → retried
    assert policy.get_active() is not None


def test_broker_healthz_reports_the_policy_version(tmp_path, monkeypatch):
    """The invariant made observable on the wire: the broker's
    /healthz says which version it serves — null while nothing is
    active, so the deny-closed state is visible, never silent. (The
    live admin-vs-broker equality assert is 7.3.6's battery.)"""
    monkeypatch.setattr(broker, "POLICY_RELOAD_SECONDS", 0.0)  # no loop in tests

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(policy, "POLICY_DIR", str(empty))
    with TestClient(broker.app) as c:
        assert c.get("/healthz").json()["policy_version"] is None

    d = _mk_store(tmp_path)
    monkeypatch.setattr(policy, "POLICY_DIR", str(d))
    with TestClient(broker.app) as c:
        body = c.get("/healthz").json()
        assert body["policy_version"] is not None
        assert body["policy_version"] == policy.get_active().version
