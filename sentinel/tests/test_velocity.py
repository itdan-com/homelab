"""ADR-007 Decision 1 — velocity as a Cedar context flag.

A synchronous count, computed by the broker from its own audit log
immediately before every Cedar evaluation, so a policy can write
`forbid ... when { context.actions_in_window._1h > N }` and have it
behave exactly like every other forbid: it trumps permit, it trumps a
held grant, and it fires at every rung (baseline/elevated/approved)
because the count is computed once and threaded into all three.

    python -m pytest tests/test_velocity.py -q
"""

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

_HERE = os.path.dirname(__file__)


def _migrate() -> None:
    cfg = Config(os.path.join(_HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_HERE, "..", "migrations"))
    command.upgrade(cfg, "head")


_migrate()

from app import ladder, policy  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AuditEvent, AuditEventType, CapabilityGrant, utcnow  # noqa: E402
from app.service import (  # noqa: E402
    actions_in_window,
    get_or_create_principal,
    mint_profile_grant,
    revoke_grant,
)

_EXAMPLE = Path(__file__).resolve().parents[1] / "policy-example"

# A read-side and a write-side velocity rule on github. Uses >= on
# purpose, not >: the window reflects calls STRICTLY BEFORE the one
# being evaluated (it is computed before this call's own audit row
# exists), so ">= N" reads as "cap total successes at N" — the Nth
# success sees a window of N-1 and still proceeds; the (N+1)th sees N
# and is the first one blocked. "> N" is also valid Cedar and a
# policy author may prefer it (it caps at N+1 instead), but it is one
# off from the intuitive "N is the limit" reading, so the tests below
# are written against >= deliberately.
_VELOCITY_OVERLAY = """
forbid(
  principal,
  action == Action::"read",
  resource
) when {
  resource.server == "github" && context.actions_in_window._1h >= 3
};
forbid(
  principal,
  action == Action::"write",
  resource
) when {
  resource.server == "github" && context.actions_in_window._1h >= 2
};
"""


@pytest.fixture(autouse=True)
def _clean_audit_log():
    """Velocity is the first thing in this suite that depends on an
    ABSOLUTE historical count rather than just current state, and every
    test file shares one on-disk SQLite database (SENTINEL_DB is set
    once per pytest SESSION — whichever test module imports first wins
    the tempdir, every later module's setdefault is a no-op) — without
    this, an earlier test's (in this file, or an earlier-run OTHER
    file's) USE rows for the same principal+tool silently inflate a
    later test's window. audit_events has no incoming foreign keys
    (it's an append-only leaf table, nothing references a row by id),
    so this delete is always safe regardless of what other files have
    written."""
    with SessionLocal() as s:
        s.execute(delete(AuditEvent))
        s.commit()
    yield


@pytest.fixture
def velocity_policy(tmp_path):
    """The committed example store, with a velocity forbid rule added
    to the overlay — kept OUT of the committed example (whose overlay
    is expected to stay empty; ADR-005 D5) and constructed here
    instead, so the feature is proven without leaving a permanent
    "schema smell" example the actual store doesn't need."""
    d = tmp_path / "store"
    d.mkdir()
    for name in ("entities.yaml", "matrix.yaml", "servers.yaml"):
        (d / name).write_text((_EXAMPLE / name).read_text())
    (d / "overlay.cedar").write_text(_VELOCITY_OVERLAY)
    return policy.activate(d, actor="velocity-test")


def _person(s, email):
    return get_or_create_principal(s, email=email)


def _read(email, n=1):
    """n birthright github reads, unclassified tier (only
    itdan-com/homelab is tier=prod per policy-example/servers.yaml).
    Re-fetches the principal INSIDE each session — an ORM instance
    crossing a closed session boundary is a documented live trap in
    this codebase (7.3, DetachedInstanceError on the first real call),
    so identity travels as a plain email string between calls here,
    the same fix already adopted elsewhere."""
    out = []
    for _ in range(n):
        with SessionLocal() as s:
            alice = _person(s, email)
            out.append(ladder.decide(
                s, principal=alice, tool="github.get_file_contents",
                arguments={"owner": "acme", "repo": "website"}))
    return out


def test_velocity_overlay_activates_and_schema_validates(velocity_policy):
    """The Cedar schema change (a REQUIRED actions_in_window record)
    doesn't just fail to break the committed example — a policy that
    actually REFERENCES the field, with the _1m/_1h field names
    app.config.VELOCITY_WINDOWS_MINUTES generates, validates and
    activates cleanly."""
    assert velocity_policy.version


def test_forbid_fires_after_the_threshold_not_before(velocity_policy):
    # 3 reads permitted (threshold is "> 3", so the 4th is the first
    # one over it) — proves the count is live-computed, not off-by-one,
    # and that staying AT the threshold still permits.
    for i, r in enumerate(_read("alice@example.com", 3)):
        assert (r.allowed, r.outcome) == (True, "permit"), f"read {i}"

    r4 = _read("alice@example.com", 1)[0]
    assert (r4.allowed, r4.outcome, r4.reason) == (False, "forbid", "forbidden")
    assert r4.hint is None, "a velocity forbid offers no elevation, same as any forbid"


def test_velocity_forbid_trumps_a_held_human_issued_grant(velocity_policy):
    """Mirrors test_forbid_trumps_possession_on_prod_tier: a tier-based
    forbid beats possession, and so must a velocity-based one — forbid
    is forbid, regardless of which context attribute triggered it."""
    with SessionLocal() as s:
        alice = _person(s, "alice@example.com")
        tools = policy.profile_tools(velocity_policy.servers, "github", "write")
        grant, _tok = mint_profile_grant(
            s, profile="github:write", tools=tools, window_minutes=30,
            granted_by="the-operator", granted_via="approve", principal=alice)
        grant_id = grant.id  # a plain string travels across sessions safely

    try:
        write = dict(tool="github.create_pull_request",
                     arguments={"owner": "acme", "repo": "website"})
        for i in range(2):
            with SessionLocal() as s:
                alice = _person(s, "alice@example.com")
                r = ladder.decide(s, principal=alice, **write)
                assert (r.allowed, r.outcome) == (True, "confirm"), f"write {i}"
        # 3rd write: threshold is "> 2" — held grant would normally
        # cover this (test_confirm_outcome_offers_elevation_then_grant_unlocks
        # proves a grant alone is sufficient), but velocity must still win.
        with SessionLocal() as s:
            alice = _person(s, "alice@example.com")
            r3 = ladder.decide(s, principal=alice, **write)
            assert (r3.allowed, r3.outcome, r3.reason) == (
                False, "forbid", "forbidden")
            assert r3.hint is None
    finally:
        # This is the one test in the file that mints a grant with a
        # real window (30 minutes) instead of letting a fixture-scoped
        # DB reset clear it — a 30-minute-live grant would otherwise
        # outlive this test and be visible to whatever runs next in
        # the same shared database (caught via
        # `pytest tests/test_velocity.py tests/test_ladder.py`, file
        # order reversed from the default alphabetical run: a later
        # ladder.py test found alice already holding this grant and
        # failed asserting on a state that presumed none existed yet).
        with SessionLocal() as s:
            g = s.get(CapabilityGrant, grant_id)
            if g is not None and g.revoked_at is None:
                revoke_grant(s, g, by="velocity-test-cleanup")


def test_only_completed_calls_count_not_denials(velocity_policy):
    """A denied attempt did nothing, so it must not count toward "stop
    action N" — otherwise a caller could be locked out by their OWN
    already-refused attempts, which is backwards. Uses hr-platform's
    write-on-approval rung: with no grant, every call denies as
    elevation/approval-required (audited as DENIAL, never USE), so if
    denials counted, enough of them would eventually trip a github rule
    for a DIFFERENT server too — they must not, on two axes at once
    (wrong tool AND not a USE event)."""
    with SessionLocal() as s:
        harriet = _person(s, "harriet@example.com")
        staging = dict(tool="hr-platform.update_record",
                       arguments={"database": "hr_staging"})
        for _ in range(6):  # comfortably past github's ">2" write threshold
            r = ladder.decide(s, principal=harriet, **staging)
            assert (r.allowed, r.reason) == (False, "approval-required")

        window = actions_in_window(s, "harriet@example.com",
                                   "hr-platform.update_record", "staging")
        assert window == {"_1m": 0, "_1h": 0}, (
            "six denied attempts must not have incremented the count")


def test_count_is_scoped_to_principal_and_tool_not_shared(velocity_policy):
    """Alice's reads on one tool must not push a DIFFERENT tool over
    its own threshold — the count is (principal, tool, tier), never
    just "recent activity on this server"."""
    # Alice reads 3 times (at, not over, the threshold).
    for r in _read("alice@example.com", 3):
        assert (r.allowed, r.outcome) == (True, "permit")

    with SessionLocal() as s:
        alice = _person(s, "alice@example.com")
        # A DIFFERENT tool, same principal, same server: untouched.
        r = ladder.decide(s, principal=alice, tool="github.list_branches",
                          arguments={"owner": "acme", "repo": "website"})
        assert (r.allowed, r.outcome) == (True, "permit")

        window_alice = actions_in_window(s, "alice@example.com",
                                         "github.get_file_contents",
                                         "unclassified")
        window_other_tool = actions_in_window(s, "alice@example.com",
                                              "github.list_branches",
                                              "unclassified")
        assert window_alice["_1h"] == 3
        assert window_other_tool["_1h"] == 1


def test_visible_tools_never_hides_behind_a_phantom_velocity_forbid(velocity_policy):
    """visible_tools() asks with a ZERO window (a hypothetical "could
    you ever" check, no real call behind it) — it must not be affected
    by a velocity rule at all, the same way it already ignores the
    concrete resource. If this used the real window, a person who had
    merely BROWSED past their threshold would see the tool vanish from
    their listing entirely, which is a materially different (and
    wrong) UX from "the next call happens to be refused"."""
    _read("alice@example.com", 5)  # 5 > the ">3" read threshold
    tools = ladder.visible_tools(velocity_policy, "alice@example.com")
    assert tools.get("github.get_file_contents") == "permit", (
        "listing must stay a policy-only question, unaffected by velocity")


def test_audit_row_carries_tier_for_the_use_event(velocity_policy):
    """The column the velocity query's index depends on actually gets
    populated on the path that matters."""
    _read("alice@example.com", 1)
    with SessionLocal() as s:
        row = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.USE,
            AuditEvent.principal == "alice@example.com",
            AuditEvent.tool == "github.get_file_contents",
        ).order_by(AuditEvent.id.desc())).first()
        assert row.tier == "unclassified"


def test_missing_context_field_denies_not_silently_permits(velocity_policy):
    """Regression guard for an adversarial-review finding: cedarpy's
    is_authorized() WITHOUT schema= does not enforce a schema's
    `required` flag at all — a context missing actions_in_window
    returned Decision.Allow outright, not an error and not a no-op,
    verified directly against the shipped cedarpy 4.8.7. _ask() now
    always passes schema=policy.SCHEMA; this proves that wiring
    actually changes the outcome by calling _ask() directly with a
    context that OMITS the required field entirely — simulating what a
    future call site forgetting to merge it in would produce — where a
    healthy, complete context would permit (alice's github read
    birthright)."""
    ok = ladder._ask(
        velocity_policy, "alice@example.com", "read",
        "github/*", "github", "unclassified",
        {})  # no actions_in_window key at all — deliberately incomplete
    assert ok is False, (
        "a context missing the required actions_in_window field must "
        "deny (NoDecision without schema enforcement this would have "
        "been Decision.Allow), never silently permit")


def test_1m_and_1h_windows_are_independent(velocity_policy):
    """Regression guard confirmed by mutation testing during review:
    every other test's calls complete within milliseconds, so a bug
    collapsing the two windows into one (e.g. a copy-paste reusing the
    same `since` variable for both) passed every other test in this
    file undetected. Inserts a USE row timestamped 30 minutes in the
    past directly — inside the _1h window, outside _1m — and proves
    the two windows actually disagree about it."""
    with SessionLocal() as s:
        s.add(AuditEvent(
            event_type=AuditEventType.USE, principal="alice@example.com",
            tool="github.get_file_contents", tier="unclassified",
            ts=utcnow() - timedelta(minutes=30)))
        s.commit()
        window = actions_in_window(s, "alice@example.com",
                                   "github.get_file_contents", "unclassified")
        assert window == {"_1m": 0, "_1h": 1}, (
            "a 30-minute-old event must count toward _1h but not _1m — "
            "if this fails with _1m == _1h, the two windows have been "
            "collapsed into one")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
