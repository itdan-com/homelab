"""7.2.3 — the four-outcome ladder in the broker: policy decides the
outcome class, possession of a live grant turns confirm/approve into a
yes, forbid trumps possession, and everything unknown denies closed.

    python -m pytest tests/test_ladder.py -q
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

from app import ladder, policy  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AuditEvent, AuditEventType, CapabilityGrant  # noqa: E402
from app.service import (  # noqa: E402
    _grant_covers,
    engage_kill,
    get_or_create_principal,
    mint_profile_grant,
    release_kill,
    revoke_grant,
)

_EXAMPLE = Path(__file__).resolve().parents[1] / "policy-example"


@pytest.fixture(autouse=True)
def example_policy(tmp_path):
    """Each test runs against a fresh activation of the committed
    example store — the module-global active policy is shared across
    test files, so pin it here rather than hoping about order."""
    d = tmp_path / "store"
    d.mkdir()
    for name in ("entities.yaml", "matrix.yaml", "servers.yaml", "overlay.cedar"):
        (d / name).write_text((_EXAMPLE / name).read_text())
    return policy.activate(d, actor="ladder-test")


def _person(s, email):
    return get_or_create_principal(s, email=email)


def test_birthright_read_is_permit_with_full_audit(example_policy):
    with SessionLocal() as s:
        alice = _person(s, "alice@example.com")
        r = ladder.decide(s, principal=alice, tool="github.get_file",
                          arguments={"repo": "acme/website"})
        assert (r.allowed, r.outcome, r.grant_id) == (True, "permit", None)
        assert r.resource == "github/acme/website"

        row = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.USE,
            AuditEvent.principal == "alice@example.com",
        ).order_by(AuditEvent.id.desc())).first()
        assert row.resource == "github/acme/website"
        assert row.policy_version == example_policy.version
        assert (row.details or {}).get("path") == "birthright"


def test_handshake_rides_birthright_at_zero_approvals(example_policy):
    """The six-approvals finding, retired: initialize/tools.list are
    read-classified via the rpc.* prefix class, so an assigned server
    is reachable with NO grant and NO console tap."""
    with SessionLocal() as s:
        alice = _person(s, "alice@example.com")
        for leaf in ("rpc.initialize", "rpc.tools.list", "rpc.transport.get"):
            r = ladder.decide(s, principal=alice, tool=f"github.{leaf}")
            assert (r.allowed, r.outcome) == (True, "permit"), leaf


def test_confirm_outcome_offers_elevation_then_grant_unlocks(example_policy):
    with SessionLocal() as s:
        alice = _person(s, "alice@example.com")
        write = dict(tool="github.create_pull_request",
                     arguments={"repo": "acme/website"})

        r = ladder.decide(s, principal=alice, **write)
        assert (r.allowed, r.outcome, r.reason) == (
            False, "confirm", "elevation-available")
        assert r.hint == {"profile": "github:write", "windows": [30, 60, 120]}

        _, _tok = mint_profile_grant(
            s, profile="github:write",
            tools=policy.profile_tools(example_policy.servers, "github", "write"),
            window_minutes=30, granted_by="alice@example.com",
            granted_via="confirm", principal=alice,
        )
        r = ladder.decide(s, principal=alice, **write)
        assert (r.allowed, r.outcome) == (True, "confirm")
        assert r.grant_id is not None

        # Revoke → the offer returns; possession is live-checked.
        g = s.get(CapabilityGrant, r.grant_id)
        revoke_grant(s, g, by="operator-test")
        r = ladder.decide(s, principal=alice, **write)
        assert (r.allowed, r.reason) == (False, "elevation-available")


def test_approve_rung_requires_human_issued_grant(example_policy):
    with SessionLocal() as s:
        harriet = _person(s, "harriet@example.com")
        staging = dict(tool="hr-platform.update_record",
                       arguments={"database": "hr_staging"})

        r = ladder.decide(s, principal=harriet, **staging)
        assert (r.allowed, r.outcome, r.reason) == (
            False, "approve", "approval-required")
        assert r.hint["profile"] == "hr-platform:write"

        # A SELF-issued grant does not satisfy the approve rung.
        tools = policy.profile_tools(example_policy.servers, "hr-platform", "write")
        _, _ = mint_profile_grant(
            s, profile="hr-platform:write", tools=tools, window_minutes=30,
            granted_by="harriet@example.com", granted_via="confirm",
            principal=harriet)
        r = ladder.decide(s, principal=harriet, **staging)
        assert (r.allowed, r.reason) == (False, "approval-required")

        # A human-issued grant (the console door) does.
        _, _ = mint_profile_grant(
            s, profile="hr-platform:write", tools=tools, window_minutes=30,
            granted_by="the-operator", granted_via="approve",
            principal=harriet)
        r = ladder.decide(s, principal=harriet, **staging)
        assert (r.allowed, r.outcome) == (True, "approve")


def test_forbid_trumps_possession_on_prod_tier(example_policy):
    """The owner's source-of-truth stance, enforced against a caller
    who HOLDS a valid human-issued grant: prod write stays forbidden."""
    with SessionLocal() as s:
        harriet = _person(s, "harriet@example.com")
        tools = policy.profile_tools(example_policy.servers, "hr-platform", "write")
        mint_profile_grant(
            s, profile="hr-platform:write", tools=tools, window_minutes=30,
            granted_by="the-operator", granted_via="approve", principal=harriet)
        r = ladder.decide(s, principal=harriet,
                          tool="hr-platform.update_record",
                          arguments={"database": "hr_prod"})
        assert (r.allowed, r.outcome, r.reason) == (False, "forbid", "forbidden")
        assert r.hint is None  # forbidden offers nothing


def test_unassigned_server_is_invisible_even_for_handshake(example_policy):
    with SessionLocal() as s:
        harriet = _person(s, "harriet@example.com")
        for tool in ("github.rpc.initialize", "github.get_file"):
            r = ladder.decide(s, principal=harriet, tool=tool,
                              arguments={"repo": "acme/website"})
            assert (r.allowed, r.outcome) == (False, "forbid"), tool


def test_unknowns_deny_closed(example_policy):
    with SessionLocal() as s:
        alice = _person(s, "alice@example.com")
        # Tool the server never classified: no guessing.
        r = ladder.decide(s, principal=alice, tool="github.delete_repository")
        assert r.reason == "unclassified-tool"
        # Resource map present but extraction fails: missing argument…
        r = ladder.decide(s, principal=alice, tool="github.create_pull_request")
        assert r.reason == "unmapped-resource"
        # …and an injection-shaped value is unmapped, never escaped.
        r = ladder.decide(s, principal=alice, tool="github.create_pull_request",
                          arguments={"repo": 'x" || true //'})
        assert r.reason == "unmapped-resource"
        # A person the STORE does not know gets forbid, whatever the DB says.
        mallory = _person(s, "mallory@example.com")
        r = ladder.decide(s, principal=mallory, tool="echo.say")
        assert (r.allowed, r.outcome) == (False, "forbid")


def test_kill_switch_beats_policy(example_policy):
    with SessionLocal() as s:
        alice = _person(s, "alice@example.com")
        engage_kill(s, by="ladder-test", reason="drill")
        try:
            r = ladder.decide(s, principal=alice, tool="github.get_file",
                              arguments={"repo": "acme/website"})
            assert (r.allowed, r.reason) == (False, "kill-engaged")
        finally:
            release_kill(s, by="ladder-test")


def test_grant_covers_prefix_classes():
    g = CapabilityGrant(tools_json=["github.rpc.*", "github.get_file"],
                        tool="profile:github:read")
    assert _grant_covers(g, "github.rpc.transport.get")
    assert _grant_covers(g, "github.rpc")          # the bare class name
    assert _grant_covers(g, "github.get_file")
    assert not _grant_covers(g, "github.rpcx")     # the dot is load-bearing
    assert not _grant_covers(g, "github.merge_pull_request")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
