"""End-to-end broker lifecycle across BOTH listeners, in-process.

Every branch a consumer can hit — the happy path, the four ways
/capability-check says no, dedupe, deny, kill-as-revocation, and
kill-survives-restart, then audit completeness. No network: FastAPI
TestClient drives the same ASGI apps uvicorn would serve. A fresh temp
DB per process via SENTINEL_DB, migrated with the real Alembic stack.

One ordered scenario (state carries forward), so it's a single test
function — a lifecycle, not independent units.

    python -m pytest tests/        # or:  python tests/test_broker_flow.py
"""

import os
import sys
import tempfile
from pathlib import Path

# Runnable directly (python tests/…) as well as under pytest: ensure the
# sentinel/ root is importable without relying on conftest.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Point the app at a throwaway DB BEFORE importing anything that binds
# the engine at import time.
os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
# TestClient sends `Host: testserver`, which the admin app's
# anti-DNS-rebinding allowlist would otherwise (correctly) refuse.
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.broker import app as broker_app  # noqa: E402
from app.main import app as admin_app  # noqa: E402
from authkit import sign_in  # noqa: E402

_HERE = os.path.dirname(__file__)
broker = TestClient(broker_app)
# https + sign-in: since 5.5.6 the console has no anonymous
# surface, so authenticating first is simply what using it looks
# like (the session cookie is Secure, which an http client drops).
admin = TestClient(admin_app, base_url="https://testserver")
CONSOLE = {"x-sentinel-console": "1"}  # the admin app's CSRF guard


def _migrate() -> None:
    cfg = Config(os.path.join(_HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_HERE, "..", "migrations"))
    command.upgrade(cfg, "head")


NONCE = "lifecycle-claim-nonce-0123456789"


def _ask(flow, tool, reason="because", agent="claude", nonce=NONCE):
    return broker.post("/v1/capability-requests",
                       json={"flow_id": flow, "tool": tool, "reason": reason,
                             "agent": agent, "claim_nonce": nonce})


def _poll(rid, nonce=NONCE):
    return broker.get(f"/v1/capability-requests/{rid}",
                      headers={"X-Claim-Nonce": nonce})


def _check(token, tool, flow):
    # Token rides in a HEADER: uvicorn logs full query strings, so a
    # token in the URL would land in journald in plaintext.
    return broker.get("/v1/capability-check",
                      params={"tool": tool, "flow_id": flow},
                      headers={"X-Sentinel-Token": token})


def test_broker_lifecycle():
    _migrate()
    sign_in(admin)

    # --- happy path -----------------------------------------------------------
    r = _ask("flow-A", "github.create_pr")
    assert r.status_code == 202 and "request_id" in r.json(), "request returns 202 + id"
    rid = r.json()["request_id"]

    assert any(p["request_id"] == rid for p in admin.get("/v1/capability-requests").json()), \
        "pending shows in admin panel"
    assert _poll(rid).json()["status"] == "pending" and "token" not in _poll(rid).json(), \
        "poll before decision has no token"

    g = admin.post(f"/v1/capability-requests/{rid}/grant",
                   json={"ttl_minutes": 5}, headers=CONSOLE)
    assert g.status_code == 201 and "token" not in g.json(), "grant 201, no token echoed"

    token = _poll(rid).json().get("token")
    assert token and token.startswith("snt_"), "first poll after grant carries token"
    assert "token" not in _poll(rid).json(), "second poll does NOT re-carry token (claim-once)"
    assert _check(token, "github.create_pr", "flow-A").status_code == 200, "check ALLOW = 200"

    # --- the four denials -----------------------------------------------------
    assert _check(token, "github.delete_repo", "flow-A").json()["reason"] == "scope-mismatch", \
        "wrong tool = scope-mismatch"
    assert _check(token, "github.create_pr", "flow-B").status_code == 403, "wrong flow = 403"
    assert _check("snt_garbage", "github.create_pr", "flow-A").json()["reason"] == "unknown-token", \
        "unknown token = unknown-token"

    # --- dedupe + deny --------------------------------------------------------
    d1, d2 = _ask("flow-C", "slack.post"), _ask("flow-C", "slack.post")
    assert d2.status_code == 200 and d2.json()["request_id"] == d1.json()["request_id"], \
        "duplicate pending dedupes to same id"
    did = d1.json()["request_id"]
    admin.post(f"/v1/capability-requests/{did}/deny", json={"reason": "nope"}, headers=CONSOLE)
    pd = _poll(did).json()
    assert pd["status"] == "denied" and pd["denied_reason"] == "nope", "denied poll carries reason"
    assert admin.post(f"/v1/capability-requests/{did}/grant",
                      json={}, headers=CONSOLE).status_code == 409, "grant on denied = 409"

    # --- kill = revocation ----------------------------------------------------
    assert _check(token, "github.create_pr", "flow-A").status_code == 200, "token live until kill"
    k = admin.post("/v1/kill", json={"reason": "drill"}, headers=CONSOLE)
    assert k.json()["grants_revoked"] >= 1, "kill revoked the live grant"
    assert _check(token, "github.create_pr", "flow-A").json()["reason"] == "kill-engaged", \
        "post-kill check = kill-engaged"
    blocked = _ask("flow-D", "x.y").json()["request_id"]
    assert admin.post(f"/v1/capability-requests/{blocked}/grant",
                      json={}, headers=CONSOLE).status_code == 409, "no new grants while killed"

    # --- kill survives a restart ----------------------------------------------
    import app.db as dbmod
    from importlib import reload
    reload(dbmod)  # brand-new engine/session, as a fresh process would build
    from app.main import app as admin2
    # Same cookies on purpose: a PROCESS restart must not sign the
    # operator out — the session is a row in the DB, not memory.
    assert TestClient(admin2, base_url="https://testserver",
                      cookies=admin.cookies).get("/v1/kill").json()["engaged"] is True, \
        "kill state (and the console session) survive a restart"

    admin.post("/v1/kill/release", headers=CONSOLE)
    assert _check(token, "github.create_pr", "flow-A").json()["reason"] == "revoked", \
        "release resumes flow; old token stays revoked"

    # --- audit completeness ---------------------------------------------------
    types = {e["event_type"] for e in admin.get("/v1/audit-events", params={"limit": 500}).json()}
    need = {"request", "grant", "denial", "use", "revocation", "kill_engaged", "kill_released"}
    assert need <= types, f"audit has all 7 event types (has {sorted(types)})"


def test_token_pickup_belongs_to_the_asker():
    """B1: dedupe used to hand any caller another caller's request_id,
    and the poll authenticated nothing — so whoever polled fastest after
    the human clicked Grant took the token. The claim nonce is what makes
    the one-time pickup belong to the caller that actually asked."""
    _migrate()
    victim_nonce, attacker_nonce = "victim-nonce-0123456789ab", "attacker-nonce-0123456789"
    v = _ask("hijack-flow", "github.create_pr", reason="open the KEDA PR",
             nonce=victim_nonce).json()["request_id"]

    # The attacker names the victim's flow and tool, hoping to be handed
    # the victim's request back. It now gets its OWN request instead...
    a = _ask("hijack-flow", "github.create_pr", reason="delete the release branch",
             agent="impostor", nonce=attacker_nonce).json()["request_id"]
    assert a != v, "a different caller does not inherit someone else's request"

    # ...and the human sees BOTH, each with its own justification, rather
    # than approving one text while a different caller collects the token.
    pending = admin.get("/v1/capability-requests").json()
    reasons = {p["reason"] for p in pending if p["request_id"] in (v, a)}
    assert reasons == {"open the KEDA PR", "delete the release branch"}, \
        "each asker's own justification reaches the approval screen"

    # Even knowing the id, the wrong nonce cannot poll it — and gets the
    # same answer as an unknown id, so it learns nothing.
    admin.post(f"/v1/capability-requests/{v}/grant",
               json={"ttl_minutes": 5}, headers=CONSOLE)
    assert _poll(v, nonce=attacker_nonce).status_code == 404, \
        "wrong nonce cannot claim, and cannot confirm the request exists"
    assert broker.get(f"/v1/capability-requests/{v}").status_code == 422, \
        "no nonce at all is a malformed poll"
    assert _poll(v, nonce=victim_nonce).json()["token"].startswith("snt_"), \
        "the caller that asked still gets its token"


def test_kill_can_be_pressed_twice():
    """A1: the sweep used to sit inside `if not ks.engaged`, so a second
    press revoked nothing. A grant that became live while engaged could
    then never be cleaned up, and came back alive on release."""
    _migrate()
    admin.post("/v1/kill/release", headers=CONSOLE)  # start from off
    n = "twice-nonce-0123456789abcd"
    rid = _ask("twice-flow", "echo.say", nonce=n).json()["request_id"]
    admin.post(f"/v1/capability-requests/{rid}/grant",
               json={"ttl_minutes": 5}, headers=CONSOLE)
    tok = _poll(rid, nonce=n).json()["token"]

    first = admin.post("/v1/kill", json={"reason": "drill"}, headers=CONSOLE).json()
    assert first["grants_revoked"] >= 1 and _check(tok, "echo.say", "twice-flow").status_code == 403

    # Second press: still allowed, still audited, and it re-sweeps.
    again = admin.post("/v1/kill", json={"reason": "again"}, headers=CONSOLE)
    assert again.status_code == 200, "pressing kill twice is not an error"
    assert again.json()["engaged"] is True
    admin.post("/v1/kill/release", headers=CONSOLE)
    assert _check(tok, "echo.say", "twice-flow").status_code == 403, \
        "release does not resurrect a revoked grant"


def test_claim_is_audited():
    """A5: the record could not answer 'was this capability actually
    picked up?' — an unclaimed grant looked identical to a used one."""
    _migrate()
    n = "audit-claim-nonce-0123456789"
    rid = _ask("claim-flow", "echo.say", nonce=n).json()["request_id"]
    admin.post(f"/v1/capability-requests/{rid}/grant",
               json={"ttl_minutes": 5}, headers=CONSOLE)
    _poll(rid, nonce=n)
    kinds = [e["event_type"] for e in
             admin.get("/v1/audit-events", params={"flow_id": "claim-flow"}).json()]
    assert "claim" in kinds, "the moment the credential left Sentinel is on the record"


if __name__ == "__main__":
    test_broker_lifecycle()
    print("PASS — broker lifecycle (20 assertions)")
