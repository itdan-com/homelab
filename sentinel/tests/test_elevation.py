"""7.3.5: the confirm and approve doors — and the measurement.

The properties under test:
  * a refused call hands back a one-time link that fixes it;
  * `confirm` is the caller elevating THEMSELVES, behind the company
    IdP, with a click the model cannot make;
  * `approve` puts a card in front of a DIFFERENT human and the grant
    it mints is the only kind that satisfies the approve rung;
  * an honest session costs at most ONE approval (5.5.8 measured six).

    python -m pytest tests/test_elevation.py -q
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
from app.models import (  # noqa: E402
    AuditEvent,
    CapabilityGrant,
    CapabilityRequest,
    Principal,
    RequestStatus,
)
from app.service import get_or_create_principal, grant_request  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]


def _migrate():
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(cfg, "head")


_migrate()

ENGINEER = "eve@example.com"      # in the store's engineering group
HR_HEAD = "harriet@example.com"   # the approve rung


@pytest.fixture(scope="module", autouse=True)
def _policy():
    d = Path(_TMP) / "store"
    d.mkdir(exist_ok=True)
    for doc in ("entities.yaml", "matrix.yaml", "servers.yaml", "overlay.cedar"):
        shutil.copy(_ROOT / "policy-example" / doc, d / doc)
    # One extra engineer so this module's grants never collide with
    # another test file's alice (the suite shares one database).
    ent = (d / "entities.yaml").read_text().replace(
        "people:", "people:\n  eve@example.com:\n"
        "    display_name: Eve\n    groups: [engineering]\n", 1)
    (d / "entities.yaml").write_text(ent)
    policy.activate(d, actor="test")
    yield


@pytest.fixture
def c():
    with TestClient(door.app) as client:
        yield client


def _token(email: str) -> str:
    with SessionLocal() as s:
        p = get_or_create_principal(s, email=email)
        pid = p.id
    now = int(time.time())
    return jwt.encode({"iss": door.DOOR_ORIGIN, "sub": pid, "email": email,
                       "aud": door.RESOURCE, "client_id": "test-client",
                       "iat": now, "exp": now + 300},
                      door.signing_key(), algorithm="RS256")


@pytest.fixture(autouse=True)
def _no_real_idp(monkeypatch):
    """The sign-in redirect asks the IdP for its endpoints; this module
    tests the door's own pages, not the federation (that is
    test_door.py), so the discovery call is stubbed rather than
    reaching a live Authentik."""
    monkeypatch.setattr(door, "oidc_config", lambda: {
        "authorization_endpoint": "https://idp.test/authorize",
        "token_endpoint": "https://idp.test/token",
        "jwks_uri": "https://idp.test/jwks"})


def _session_cookie(email: str) -> str:
    with SessionLocal() as s:
        p = get_or_create_principal(s, email=email)
        return door._session_cookie(p.id, p.email)


def _call(c, email, name, arguments):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}},
                  headers={"Authorization": f"Bearer {_token(email)}"})


def _ticket_from_refusal(c, email, name, arguments) -> tuple[str, dict]:
    body = _call(c, email, name, arguments).json()
    elev = body["error"]["data"]["elevation"]
    return elev["url"].rsplit("/", 1)[1], elev


def _revoke_all(email: str) -> None:
    from app.service import revoke_grant
    with SessionLocal() as s:
        p = s.scalars(select(Principal).where(Principal.email == email)).first()
        for g in s.scalars(select(CapabilityGrant).where(
                CapabilityGrant.principal_id == p.id,
                CapabilityGrant.revoked_at.is_(None))).all():
            revoke_grant(s, g, by="test", reason="cleanup")


# --- the link ----------------------------------------------------------------

def test_a_refusal_hands_back_a_one_time_elevation_link(c):
    ticket, elev = _ticket_from_refusal(
        c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
    assert elev["profile"] == "github:write"
    assert elev["url"].startswith(f"{door.DOOR_ORIGIN}/elevate/")
    assert door._tickets[ticket]["outcome"] == "confirm"


def test_the_refusal_carries_the_link_in_its_MESSAGE(c):
    """MCP clients surface the message string and may drop the
    structured payload, so a refusal that names a remedy has to carry
    it inline — otherwise the caller is told to open a link it was
    never given (found live, 2026-08-02)."""
    body = _call(c, ENGINEER, "github.create_pull_request",
                 {"owner": "itdan-com", "repo": "other"}).json()
    err = body["error"]
    assert err["data"]["elevation"]["url"] in err["message"]
    assert "elevation.url" not in err["message"]  # no dangling field reference


def test_the_page_requires_signing_in_first(c):
    """No cookie ⇒ the door sends you to the company IdP. The MODEL
    holds an API token, not a browser session, so it cannot follow
    this path — which is the whole point of putting elevation here."""
    ticket, _ = _ticket_from_refusal(
        c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
    r = c.get(f"/elevate/{ticket}", follow_redirects=False)
    assert r.status_code == 302
    assert "authorize" in r.headers["location"]


def test_another_person_cannot_use_your_link(c):
    ticket, _ = _ticket_from_refusal(
        c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
    r = c.get(f"/elevate/{ticket}",
              cookies={"door_session": _session_cookie(HR_HEAD)})
    assert r.status_code == 403


def test_a_forged_form_post_is_refused(c):
    ticket, _ = _ticket_from_refusal(
        c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
    def confirm_grants() -> int:
        with SessionLocal() as s:
            p = s.scalars(select(Principal).where(
                Principal.email == ENGINEER)).first()
            return len(s.scalars(select(CapabilityGrant).where(
                CapabilityGrant.principal_id == p.id,
                CapabilityGrant.granted_via == "confirm")).all())

    before = confirm_grants()
    r = c.post(f"/elevate/{ticket}", data={"csrf": "guess", "minutes": 30},
               cookies={"door_session": _session_cookie(ENGINEER)})
    assert r.status_code == 400
    assert confirm_grants() == before  # nothing minted, and the ticket survives


def test_only_an_offered_window_is_accepted(c):
    """The windows come from the matrix; a hand-typed 10-hour window is
    not a negotiation."""
    ticket, _ = _ticket_from_refusal(
        c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
    page = c.get(f"/elevate/{ticket}",
                 cookies={"door_session": _session_cookie(ENGINEER)})
    csrf = page.text.split('name=csrf value=')[1].split('>')[0].strip()
    r = c.post(f"/elevate/{ticket}", data={"csrf": csrf, "minutes": 600},
               cookies={"door_session": _session_cookie(ENGINEER)})
    assert r.status_code == 400


# --- confirm: the caller unlocks it themselves --------------------------------

def _confirm(c, email, ticket, minutes=30):
    cookies = {"door_session": _session_cookie(email)}
    page = c.get(f"/elevate/{ticket}", cookies=cookies)
    assert page.status_code == 200, page.text
    csrf = page.text.split('name=csrf value=')[1].split('>')[0].strip()
    return c.post(f"/elevate/{ticket}", data={"csrf": csrf, "minutes": minutes},
                  cookies=cookies)


def test_confirm_unlocks_the_call_and_the_window_is_recorded(c):
    try:
        ticket, _ = _ticket_from_refusal(
            c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
        assert "error" in _call(c, ENGINEER, "github.create_pull_request",
                                {"owner": "itdan-com", "repo": "other"}).json()

        r = _confirm(c, ENGINEER, ticket, minutes=60)
        assert r.status_code == 200 and "60 minutes" in r.text

        assert "error" not in _call(c, ENGINEER, "github.create_pull_request",
                                    {"owner": "itdan-com", "repo": "other"}).json()
        with SessionLocal() as s:
            g = s.scalars(select(CapabilityGrant).where(
                CapabilityGrant.granted_via == "confirm").order_by(
                CapabilityGrant.granted_at.desc())).first()
            assert g.profile == "github:write"
            assert g.granted_by == ENGINEER      # self-issued, and it says so
            assert "github.create_pull_request" in g.tools_json
    finally:
        _revoke_all(ENGINEER)


def test_a_link_works_once(c):
    try:
        ticket, _ = _ticket_from_refusal(
            c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
        assert _confirm(c, ENGINEER, ticket).status_code == 200
        assert c.get(f"/elevate/{ticket}",
                     cookies={"door_session": _session_cookie(ENGINEER)}
                     ).status_code == 404
    finally:
        _revoke_all(ENGINEER)


def test_self_elevation_does_not_open_the_approve_rung(c):
    """The carve that makes `approve` mean something: a grant you
    issued yourself satisfies `confirm`, never `approve` — otherwise
    every high-risk tool would be one self-service click away."""
    try:
        ticket, _ = _ticket_from_refusal(
            c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
        _confirm(c, ENGINEER, ticket)
        # harriet's rung is approve; prove a confirm-grant cannot reach it
        t2, elev = _ticket_from_refusal(
            c, HR_HEAD, "hr-platform.update_record", {"database": "hr_staging"})
        assert door._tickets[t2]["outcome"] == "approve"
        assert "Request approval" in c.get(
            f"/elevate/{t2}", cookies={"door_session": _session_cookie(HR_HEAD)}).text
    finally:
        _revoke_all(ENGINEER)


# --- approve: a different human decides ---------------------------------------

def test_approve_door_files_a_card_and_the_console_grant_unlocks_it(c):
    """One primitive, two doors: the same 5.5 card and button, with
    `granted_via=approve` and the APPROVER's name on it."""
    try:
        ticket, _ = _ticket_from_refusal(
            c, HR_HEAD, "hr-platform.update_record", {"database": "hr_staging"})
        cookies = {"door_session": _session_cookie(HR_HEAD)}
        page = c.get(f"/elevate/{ticket}", cookies=cookies)
        csrf = page.text.split('name=csrf value=')[1].split('>')[0].strip()
        r = c.post(f"/elevate/{ticket}", data={"csrf": csrf, "minutes": 30},
                   cookies=cookies)
        assert r.status_code == 200 and "approval" in r.text.lower()

        # still refused: asking is not getting
        assert "error" in _call(c, HR_HEAD, "hr-platform.update_record",
                                {"database": "hr_staging"}).json()

        with SessionLocal() as s:
            req = s.scalars(select(CapabilityRequest).where(
                CapabilityRequest.profile == "hr-platform:write",
                CapabilityRequest.status == RequestStatus.PENDING)).first()
            assert req is not None and req.principal_id
            g = grant_request(s, req, ttl_minutes=30, granted_by="operator-with-passkey")
            assert g.granted_via == "approve"
            assert g.granted_by == "operator-with-passkey"   # a DIFFERENT human
            assert s.get(CapabilityRequest, req.id).status == RequestStatus.GRANTED

        assert "error" not in _call(c, HR_HEAD, "hr-platform.update_record",
                                    {"database": "hr_staging"}).json()
    finally:
        _revoke_all(HR_HEAD)


def test_approval_never_reaches_a_forbidden_tier(c):
    """The owner's source-of-truth stance survives the approve door: a
    granted window on hr-platform still cannot write prod."""
    try:
        ticket, _ = _ticket_from_refusal(
            c, HR_HEAD, "hr-platform.update_record", {"database": "hr_staging"})
        cookies = {"door_session": _session_cookie(HR_HEAD)}
        page = c.get(f"/elevate/{ticket}", cookies=cookies)
        csrf = page.text.split('name=csrf value=')[1].split('>')[0].strip()
        c.post(f"/elevate/{ticket}", data={"csrf": csrf, "minutes": 30},
               cookies=cookies)
        with SessionLocal() as s:
            req = s.scalars(select(CapabilityRequest).where(
                CapabilityRequest.profile == "hr-platform:write",
                CapabilityRequest.status == RequestStatus.PENDING)).first()
            grant_request(s, req, ttl_minutes=30, granted_by="operator-with-passkey")
        body = _call(c, HR_HEAD, "hr-platform.update_record",
                     {"database": "hr_prod"}).json()
        assert body["error"]["data"]["outcome"] == "forbid"
    finally:
        _revoke_all(HR_HEAD)


def test_the_doors_own_forwarding_token_is_not_authority(c):
    """The sharpest regression in this phase. After an allowed call the
    door mints itself a 30-second single-tool token so the proxy will
    carry the call (7.3.6). It is hung on the same principal and names
    the same tool, so until this fix it satisfied the ladder's approve
    rung — one approved call permitted the same tool for 30 more
    seconds, and every call inside that window renewed it. An approval
    that extends itself is exactly the self-granting hole the whole
    design exists to close."""
    from app.service import mint_forwarding_token
    with SessionLocal() as s:
        p = get_or_create_principal(s, email=HR_HEAD)
        mint_forwarding_token(s, flow_id="person-regression", principal=p,
                              tool="hr-platform.update_record")
    body = _call(c, HR_HEAD, "hr-platform.update_record",
                 {"database": "hr_staging"}).json()
    assert "error" in body, "a machine-issued forwarding token granted authority"
    assert body["error"]["data"]["outcome"] == "approve"
    assert body["error"]["data"]["reason"] == "approval-required"


# --- the measurement ----------------------------------------------------------

def test_an_honest_session_costs_at_most_one_approval(c):
    """5.5.8 measured SEVEN human approvals for one MCP session. The
    same session now: sign in once (already counted), handshake, list,
    call birthright tools — ZERO. One deliberate elevation covers the
    whole write window."""
    try:
        taps = {"n": 0}
        tok = _token(ENGINEER)

        def rpc(method, params=None):
            body = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params:
                body["params"] = params
            return c.post("/mcp", json=body,
                          headers={"Authorization": f"Bearer {tok}"})

        assert "result" in rpc("initialize", {"protocolVersion": "2025-11-25"}).json()
        assert "result" in rpc("tools/list").json()
        for _ in range(3):
            assert "result" in rpc("tools/call", {
                "name": "github.get_file_contents",
                "arguments": {"owner": "itdan-com", "repo": "other"}}).json()
        assert taps["n"] == 0, "birthright must cost nothing"

        # one write ⇒ one human act, covering every write in the window
        ticket, _ = _ticket_from_refusal(
            c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
        _confirm(c, ENGINEER, ticket, minutes=30)
        taps["n"] += 1
        for _ in range(3):
            assert "error" not in rpc("tools/call", {
                "name": "github.create_pull_request",
                "arguments": {"owner": "itdan-com", "repo": "other"}}).json()
        assert taps["n"] == 1
    finally:
        _revoke_all(ENGINEER)


def test_the_audit_log_reconstructs_the_window(c):
    """'Recording rich enough to reconstruct what happened inside an
    elevation window' — the grant, its via, and every call under it."""
    try:
        ticket, _ = _ticket_from_refusal(
            c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
        _confirm(c, ENGINEER, ticket, minutes=30)
        _call(c, ENGINEER, "github.create_pull_request", {"owner": "itdan-com", "repo": "other"})
        with SessionLocal() as s:
            rows = s.scalars(select(AuditEvent).where(
                AuditEvent.principal == ENGINEER)).all()
        kinds = [r.event_type.value for r in rows]
        assert "grant" in kinds and "denial" in kinds and "use" in kinds
        grant_rows = [r for r in rows if r.event_type.value == "grant"]
        assert grant_rows[-1].details["via"] == "confirm"
        assert grant_rows[-1].details["window_minutes"] == 30
        uses = [r for r in rows if r.event_type.value == "use"
                and r.tool == "github.create_pull_request"]
        assert uses and all(u.policy_version == policy.get_active().version
                            for u in uses)
    finally:
        _revoke_all(ENGINEER)
