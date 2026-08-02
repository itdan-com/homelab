"""7.2.2 (ADR-005 D5): the policy store — generation shape, semantic
refusals (including the Cedar-literal injection guard), atomic
activation with last-good-stays-live, the auto-committed git history,
and the crown assert: the ADR's four-outcome ladder proven against
GENERATED policy, not the hand-written proof from 7.1.

    python -m pytest tests/test_policy_store.py -q
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

import pytest  # noqa: E402
from cedarpy import Decision, is_authorized  # noqa: E402

from app import policy  # noqa: E402
from app.policy import (  # noqa: E402
    PolicyError,
    activate,
    classify_tool,
    load_store,
    generate,
    profile_tools,
)

_EXAMPLE = Path(__file__).resolve().parents[1] / "policy-example"


def _mk_store(tmp: Path, **overrides: str) -> Path:
    """A store dir seeded from the committed example, with optional
    per-document overrides — the example store IS the fixture, so the
    docs and the tests cannot drift apart."""
    d = Path(tmp) / f"store-{uuid.uuid4().hex[:6]}"
    d.mkdir()
    for name in ("entities.yaml", "matrix.yaml", "servers.yaml", "overlay.cedar"):
        content = overrides.get(name.split(".")[0].replace("-", "_"))
        (d / name).write_text(content if content is not None
                              else (_EXAMPLE / name).read_text())
    return d


def _outcome(ap, principal, action, resource_id, server, tier):
    """The broker ladder in miniature (ADR-005 Decision 4)."""
    entities = json.loads(ap.entities_json) + [{
        "uid": {"type": "Resource", "id": resource_id},
        "attrs": {"server": server, "tier": tier}, "parents": [],
    }]

    def ask(ctx):
        req = {"principal": f'User::"{principal}"',
               "action": f'Action::"{action}"',
               "resource": f'Resource::"{resource_id}"', "context": ctx}
        return is_authorized(req, ap.policies, entities).decision == Decision.Allow

    if ask({}):
        return "permit"
    if ask({"elevated": True}):
        return "confirm"
    if ask({"approved": True}):
        return "approve"
    return "forbid"


def test_generation_shape(tmp_path):
    groups, people, matrix, servers, overlay, _ = load_store(_mk_store(tmp_path))
    policies, entities_json = generate(groups, people, matrix, overlay)

    assert 'principal in Group::"all-employees"' in policies       # birthright
    assert 'context has elevated && context.elevated == true' in policies
    assert 'context has approved && context.approved == true' in policies
    assert 'forbid(' in policies and 'resource.tier == "prod"' in policies

    entities = json.loads(entities_json)
    alice = next(e for e in entities if e["uid"]["id"] == "alice@example.com")
    assert {"type": "Group", "id": "all-employees"} in alice["parents"]
    assert {"type": "Group", "id": "engineering"} in alice["parents"]
    hr_head = next(e for e in entities if e["uid"]["id"] == "hr-head")
    assert hr_head["parents"] == [{"type": "Group", "id": "hr"}]


def test_four_outcomes_against_generated_policy(tmp_path):
    ap = activate(_mk_store(tmp_path), actor="test")

    # Engineering: birthright read, borrowable write (confirm).
    assert _outcome(ap, "alice@example.com", "read",
                    "github/x", "github", "staging") == "permit"
    assert _outcome(ap, "alice@example.com", "write",
                    "github/x", "github", "staging") == "confirm"
    # all-employees birthright reaches alice implicitly.
    assert _outcome(ap, "alice@example.com", "read",
                    "echo/-", "echo", "unclassified") == "permit"
    # HR never assigned github: can't even ask (deny at every rung).
    assert _outcome(ap, "harriet@example.com", "read",
                    "github/x", "github", "staging") == "forbid"
    # hr-head inherits hr's read birthright on hr-platform (lattice)...
    assert _outcome(ap, "harriet@example.com", "read",
                    "hr-platform/hr_staging", "hr-platform", "staging") == "permit"
    # ...and holds the high-risk rung on staging (a different human)...
    assert _outcome(ap, "harriet@example.com", "write",
                    "hr-platform/hr_staging", "hr-platform", "staging") == "approve"
    # ...while prod write is FORBIDDEN through every context — the
    # owner's source-of-truth stance as an engine property.
    assert _outcome(ap, "harriet@example.com", "write",
                    "hr-platform/hr_prod", "hr-platform", "prod") == "forbid"


@pytest.mark.parametrize("mutation, fragment", [
    ({"matrix": "grants:\n  engineering:\n    github:\n      level: sudo\n"},
     "bad level"),
    ({"matrix": "grants:\n  ghosts:\n    github:\n      level: read\n"},
     "unknown group"),
    ({"matrix": "grants:\n  engineering:\n    nowhere:\n      level: read\n"},
     "unknown server"),
    ({"entities": "groups:\n  a:\n    parent: b\n  b:\n    parent: a\n"},
     "cycle"),
    ({"entities": 'people:\n  bad"guy@x.com:\n    groups: []\n'},
     "Cedar literals"),
    ({"matrix": "forbids:\n  - server: nowhere\n"}, "unknown server"),
])
def test_semantic_refusals(tmp_path, mutation, fragment):
    with pytest.raises(PolicyError) as e:
        load_store(_mk_store(tmp_path, **mutation))
    assert any(fragment in err for err in e.value.errors), e.value.errors


def test_activation_versioning_and_git_history(tmp_path):
    d = _mk_store(tmp_path)
    ap1 = activate(d, actor="test")
    assert len(ap1.version) == 12
    assert (d / "generated" / "policies.cedar").exists()
    assert (d / "generated" / "entities.json").exists()

    def commits():
        r = subprocess.run(["git", "-C", str(d), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True)
        return int(r.stdout.strip())

    assert commits() == 1
    # Unchanged store: same version, no new commit.
    ap2 = activate(d, actor="test")
    assert ap2.version == ap1.version and commits() == 1
    # A real edit: new version, new commit.
    (d / "matrix.yaml").write_text(
        (d / "matrix.yaml").read_text().replace("level: write-on-request",
                                                "level: write"))
    ap3 = activate(d, actor="test")
    assert ap3.version != ap1.version and commits() == 2
    msg = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert ap3.version in msg and "by test" in msg


def test_broken_edit_keeps_last_good_live(tmp_path):
    d = _mk_store(tmp_path)
    good = activate(d, actor="test")
    (d / "matrix.yaml").write_text(
        "grants:\n  engineering:\n    github:\n      level: sudo\n")
    with pytest.raises(PolicyError):
        activate(d, actor="test")
    assert policy.get_active() is not None
    assert policy.get_active().version == good.version


def test_classification_and_profiles(tmp_path):
    ap = activate(_mk_store(tmp_path), actor="test")
    s = ap.servers
    assert classify_tool(s, "github", "get_file") == "read"
    assert classify_tool(s, "github", "create_pull_request") == "write"
    assert classify_tool(s, "github", "rpc.tools.list") == "read"   # prefix class
    assert classify_tool(s, "github", "rpc.transport.get") == "read"
    assert classify_tool(s, "github", "delete_repository") is None  # unknown → deny closed
    assert classify_tool(s, "nowhere", "anything") is None

    read = profile_tools(s, "github", "read")
    write = profile_tools(s, "github", "write")
    assert "github.get_file" in read and "github.rpc.*" in read
    assert "github.create_pull_request" not in read
    assert set(read) < set(write)  # write profile covers read too


def test_status_endpoint_reports_active_version(tmp_path):
    from starlette.testclient import TestClient
    from app.main import app as admin_app
    from authkit import sign_in

    ap = activate(_mk_store(tmp_path), actor="test")
    admin = TestClient(admin_app, base_url="https://testserver")
    sign_in(admin, username=f"pol-{uuid.uuid4().hex[:6]}", label="policy-key")
    r = admin.get("/v1/policy/status", headers={"x-sentinel-console": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True and body["version"] == ap.version
    assert "github" in body["servers"] and "engineering" in body["matrix_groups"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
