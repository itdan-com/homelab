"""7.3.4: the door speaks MCP, and the ladder decides every message.

The properties under test are the phase's promises, not the plumbing:
a signed-in person reaches their birthright tools with ZERO approvals;
a server they are not assigned is invisible, not merely refused; a
tool that needs borrowing is listed WITH what borrowing would take;
and a refusal reconstructs itself in the audit log.

    python -m pytest tests/test_door_mcp.py -q
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(_TMP, "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")
os.environ.setdefault("SENTINEL_DOOR_KEY", os.path.join(_TMP, "door-key.pem"))
os.environ.setdefault("SENTINEL_DOOR_ORIGIN", "https://mcp.test.local")
os.environ.setdefault("SENTINEL_POLICY_RELOAD_SECONDS", "0")

import jwt  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import door, policy  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AuditEvent, CapabilityGrant, Principal  # noqa: E402
from app.service import (  # noqa: E402
    get_or_create_principal,
    mint_profile_grant,
    revoke_grant,
)

_ROOT = Path(__file__).resolve().parents[1]


def _migrate():
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(cfg, "head")


_migrate()


@pytest.fixture(scope="module", autouse=True)
def _policy():
    d = Path(_TMP) / "store"
    d.mkdir(exist_ok=True)
    for doc in ("entities.yaml", "matrix.yaml", "servers.yaml", "overlay.cedar"):
        shutil.copy(_ROOT / "policy-example" / doc, d / doc)
    policy.activate(d, actor="test")
    yield


@pytest.fixture
def c():
    with TestClient(door.app) as client:
        yield client


def _token(email: str) -> str:
    """A door token for a person, minted the same way /token does.

    `idp_sub=None` deliberately: the whole suite shares one SQLite file
    (the first module's SENTINEL_DB wins), so pinning a subject here
    would collide with the sign-in tests' pins and trip the real TOFU
    guard — which is tested where it belongs, in test_door.py."""
    with SessionLocal() as s:
        p = get_or_create_principal(s, email=email)
        pid = p.id
    now = int(time.time())
    return jwt.encode({"iss": door.DOOR_ORIGIN, "sub": pid, "email": email,
                       "aud": door.RESOURCE, "client_id": "test-client",
                       "iat": now, "exp": now + 300},
                      door.signing_key(), algorithm="RS256")


def _rpc(c, token: str, method: str, params: dict | None = None, rid: int = 1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return c.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})


# --- the handshake ------------------------------------------------------------

def test_initialize_costs_zero_approvals(c):
    """The six-approvals finding, retired on the wire: a signed-in
    person completes the MCP handshake without a single human tap."""
    r = _rpc(c, _token("alice@example.com"), "initialize",
             {"protocolVersion": "2025-11-25", "capabilities": {}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-11-25"   # the client's version, echoed
    assert res["capabilities"]["tools"] == {"listChanged": False}


def test_notifications_get_no_body(c):
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={"Authorization": f"Bearer {_token('alice@example.com')}"})
    assert r.status_code == 202 and not r.content


# --- visibility: what each role SEES -----------------------------------------

def _tools(c, email):
    r = _rpc(c, _token(email), "tools/list")
    assert r.status_code == 200, r.text
    return {t["name"]: t["_meta"]["airlock/outcome"]
            for t in r.json()["result"]["tools"]}


def test_engineering_sees_its_tools_and_hr_is_invisible(c):
    """'Nobody sees what they have no business seeing' — hr-platform is
    absent from alice's listing entirely, not listed-and-refused."""
    tools = _tools(c, "alice@example.com")
    assert tools["echo.say"] == "permit"              # birthright, zero approvals
    assert tools["github.get_file"] == "permit"       # write-on-request implies read
    assert tools["github.create_pull_request"] == "confirm"  # borrowable, and says so
    assert not [t for t in tools if t.startswith("hr-platform.")]


def test_hr_head_sees_the_approve_rung(c):
    tools = _tools(c, "harriet@example.com")
    assert tools["hr-platform.lookup_employee"] == "permit"
    assert tools["hr-platform.update_record"] == "approve"
    assert not [t for t in tools if t.startswith("github.")]
    assert tools["echo.say"] == "permit"  # the all-employees birthright


def test_a_person_unknown_to_the_store_sees_nothing_at_all(c):
    """ADR-005 P1 at the visibility layer: authentication is NOT
    authorization. A valid door token for someone the policy store has
    never heard of lists zero tools — not even the `all-employees`
    birthright, because that group's membership is a fact of the
    store, not of the IdP. Onboarding is a policy-store edit (the
    console's Access screen), and until it happens an SSO login buys
    nothing."""
    assert _tools(c, "stranger@example.com") == {}


def test_transport_plumbing_is_never_a_listed_tool(c):
    assert not [t for t in _tools(c, "alice@example.com") if ".rpc" in t]


# --- calling ------------------------------------------------------------------

def test_birthright_call_is_allowed_and_needs_no_grant(c):
    r = _rpc(c, _token("alice@example.com"), "tools/call",
             {"name": "echo.say", "arguments": {"text": "hi"}})
    body = r.json()
    # Allowed by policy; no upstream is configured in tests, and the
    # door says exactly that rather than pretending it worked.
    assert "error" not in body
    assert body["result"]["isError"] is True
    assert "no upstream is configured" in body["result"]["content"][0]["text"]


def test_a_borrowable_refusal_carries_the_offer(c):
    """The refusal is the product: a client can turn this into 'unlock
    for 30 minutes?' instead of a dead end."""
    r = _rpc(c, _token("alice@example.com"), "tools/call",
             {"name": "github.create_pull_request",
              "arguments": {"repo": "itdan-com/other"}})
    err = r.json()["error"]
    assert err["data"]["outcome"] == "confirm"
    assert err["data"]["reason"] == "elevation-available"
    assert err["data"]["elevation"]["profile"] == "github:write"
    assert err["data"]["elevation"]["windows"] == [30, 60, 120]


def test_holding_a_grant_turns_the_refusal_into_a_call(c):
    """Possession, end to end through the door: mint the elevation the
    offer described and the same call goes through."""
    email = "alice@example.com"
    with SessionLocal() as s:
        p = s.scalars(select(Principal).where(Principal.email == email)).first()
        grant, _tok = mint_profile_grant(
            s, principal=p, profile="github:write",
            tools=policy.profile_tools(policy.get_active().servers,
                                       "github", "write"),
            window_minutes=30, granted_via="confirm", granted_by=email)
        gid = grant.id
    try:
        r = _rpc(c, _token(email), "tools/call",
                 {"name": "github.create_pull_request",
                  "arguments": {"repo": "itdan-com/other"}})
        assert "error" not in r.json(), r.text
    finally:
        # Live authority must not leak out of the test that minted it:
        # the suite shares one database, and a lingering grant makes
        # every later "offer" assertion pass for the wrong reason.
        with SessionLocal() as s:
            revoke_grant(s, s.get(CapabilityGrant, gid), by="test",
                         reason="test cleanup")


def test_prod_tier_write_stays_forbidden_for_hr_head(c):
    """The owner's source-of-truth stance, on the wire: no window, no
    approval, no button — and the staging twin still works."""
    r = _rpc(c, _token("harriet@example.com"), "tools/call",
             {"name": "hr-platform.update_record",
              "arguments": {"database": "hr_prod"}})
    err = r.json()["error"]
    assert err["data"]["outcome"] == "forbid"
    assert "elevation" not in err["data"]

    r = _rpc(c, _token("harriet@example.com"), "tools/call",
             {"name": "hr-platform.update_record",
              "arguments": {"database": "hr_staging"}})
    assert r.json()["error"]["data"]["outcome"] == "approve"  # borrowable, not free


def test_unassigned_server_call_is_refused(c):
    r = _rpc(c, _token("alice@example.com"), "tools/call",
             {"name": "hr-platform.lookup_employee",
              "arguments": {"database": "hr_staging"}})
    assert r.json()["error"]["data"]["outcome"] == "forbid"


def test_every_decision_lands_in_the_audit_log_with_its_policy_version(c):
    email = "auditor@example.com"
    _rpc(c, _token(email), "tools/list")
    _rpc(c, _token(email), "tools/call",
         {"name": "hr-platform.update_record", "arguments": {"database": "hr_prod"}})
    with SessionLocal() as s:
        rows = s.scalars(select(AuditEvent).where(
            AuditEvent.principal == email)).all()
    kinds = {(r.event_type.value, r.tool) for r in rows}
    assert ("use", "rpc.tools_list") in kinds
    assert ("denial", "hr-platform.update_record") in kinds
    assert all(r.policy_version == policy.get_active().version
               for r in rows if r.tool)


# --- boundaries ---------------------------------------------------------------

def test_no_server_initiated_stream(c):
    """No SSE by design (ADR-005 audit gap 1): with no open stream,
    nothing can outlive the policy that authorized it."""
    r = c.get("/mcp", headers={
        "Authorization": f"Bearer {_token('alice@example.com')}"})
    assert r.status_code == 405


def test_unknown_method_is_a_clean_jsonrpc_error(c):
    r = _rpc(c, _token("alice@example.com"), "resources/list")
    assert r.json()["error"]["code"] == -32601


def test_batch_requests_are_answered_as_a_batch(c):
    t = _token("alice@example.com")
    r = c.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             {"jsonrpc": "2.0", "method": "notifications/initialized"},
                             {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
               headers={"Authorization": f"Bearer {t}"})
    assert [m["id"] for m in r.json()] == [1, 2]  # the notification gets no reply
