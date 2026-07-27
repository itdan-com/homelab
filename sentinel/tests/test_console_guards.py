"""The admin console's three CSRF/rebinding layers, and the rule that
the operator identity is resolved server-side.

Why this file exists: the console is a WEB PAGE that holds the kill
switch, and a browser will carry a request from any tab to
127.0.0.1. "It only listens on loopback" is not a defence against the
operator's own browser — so the layers below are load-bearing, and an
untested security control is a decorative one.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")
# NB: SENTINEL_OPERATOR is deliberately NOT set here. app.config reads
# the environment once, at import — and under a full-suite run a sibling
# module imports it first, so this file must assert against the value
# the server actually resolved rather than one it hoped to inject.

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.broker import app as broker_app  # noqa: E402
from app.config import OPERATOR  # noqa: E402
from app.main import app as admin_app  # noqa: E402
from authkit import sign_in  # noqa: E402

_HERE = os.path.dirname(__file__)
broker = TestClient(broker_app)
# https + sign-in: since 5.5.6 the console has no anonymous
# surface, so authenticating first is simply what using it looks
# like (the session cookie is Secure, which an http client drops).
admin = TestClient(admin_app, base_url="https://testserver")
CONSOLE = {"x-sentinel-console": "1"}


def _migrate() -> None:
    cfg = Config(os.path.join(_HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_HERE, "..", "migrations"))
    command.upgrade(cfg, "head")


def _pending(flow="guard-flow", tool="guard.tool"):
    return broker.post("/v1/capability-requests",
                       json={"flow_id": flow, "tool": tool,
                             "reason": "guard test", "agent": "pytest",
                             "claim_nonce": "guard-claim-nonce-0123456789"}
                       ).json()["request_id"]


def test_console_guards():
    _migrate()
    sign_in(admin)
    rid = _pending()

    # Layer 3 — the custom header. A cross-origin page cannot set it
    # without a preflight this app never answers.
    assert admin.post(f"/v1/capability-requests/{rid}/grant",
                      json={"ttl_minutes": 5}).status_code == 403, \
        "grant without the console header is refused"
    assert admin.post("/v1/kill", json={}).status_code == 403, \
        "kill without the console header is refused"
    assert admin.post("/v1/kill/release").status_code == 403, \
        "release without the console header is refused"

    # Layer 2 — Origin. Header present but the request came from a page
    # on some other site: refuse even though it reached us.
    assert admin.post(f"/v1/capability-requests/{rid}/grant",
                      json={"ttl_minutes": 5},
                      headers={**CONSOLE, "origin": "https://evil.example"}
                      ).status_code == 403, "cross-origin grant is refused"
    assert admin.get("/v1/audit-events",
                     headers={"origin": "https://evil.example"}
                     ).status_code == 403, "cross-origin READ is refused too"

    # Layer 1 — Host. DNS rebinding: the attacker's name resolves to
    # 127.0.0.1, so the packet arrives, but the Host header gives it away.
    assert admin.get("/healthz",
                     headers={"host": "evil.example"}).status_code == 400, \
        "DNS-rebinding Host is refused before any route runs"

    # Same-origin console request: allowed.
    ok = admin.post(f"/v1/capability-requests/{rid}/grant", json={"ttl_minutes": 5},
                    headers={**CONSOLE, "origin": "http://testserver"})
    assert ok.status_code == 201, "the console's own request goes through"

    # Identity is the SERVER's, not the caller's: a body that tries to
    # name the approver is rejected outright rather than silently ignored.
    rid2 = _pending(tool="guard.tool2")
    assert admin.post(f"/v1/capability-requests/{rid2}/grant",
                      json={"ttl_minutes": 5, "granted_by": "not-bob"},
                      headers=CONSOLE).status_code == 422, \
        "caller-supplied actor is refused, not quietly dropped"

    events = admin.get("/v1/audit-events", params={"flow_id": "guard-flow"},
                       headers=CONSOLE).json()
    grants = [e for e in events if e["event_type"] == "grant"]
    assert grants and all(e["actor"] == OPERATOR for e in grants), \
        "the audit log names the operator Sentinel resolved, not one that was typed"
    assert all(e["actor"] != "not-bob" for e in events), \
        "nothing a caller typed ever reached the record"

    # Security headers ride on every response, and the console is served.
    page = admin.get("/")
    assert page.status_code == 200 and "SENTINEL" in page.text
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp, \
        "the page that holds the kill switch loads nothing from anywhere else"
    assert page.headers["x-content-type-options"] == "nosniff"


if __name__ == "__main__":
    test_console_guards()
    print("ok")


def test_refuses_to_start_exposed_without_tls():
    """A `--host 0.0.0.0` typo in a unit file would put the kill switch
    on the network in cleartext, and everything would still look
    healthy. Nothing else in the system catches that, so the process
    refuses to start. The rule is not 'loopback forever' — in cloud the
    console IS reachable — it is 'not exposed without https'."""
    import importlib
    import os

    import app.main

    def boots(bind, origin):
        os.environ["SENTINEL_ADMIN_BIND"] = bind
        os.environ["SENTINEL_CONSOLE_ORIGIN"] = origin
        importlib.reload(app.config)
        mod = importlib.reload(app.main)
        try:
            with TestClient(mod.app):
                return True
        except RuntimeError:
            return False

    import app.config
    original = (os.environ.get("SENTINEL_ADMIN_BIND"),
                os.environ.get("SENTINEL_CONSOLE_ORIGIN"))
    try:
        assert boots("127.0.0.1", "http://localhost:8400") is True, \
            "loopback over http is the normal local install"
        assert boots("0.0.0.0", "http://localhost:8400") is False, \
            "exposed over http refuses to start"
        assert boots("10.1.2.3", "https://sentinel.example.com") is True, \
            "exposed over https is the cloud install and is allowed"
    finally:
        for k, v in zip(("SENTINEL_ADMIN_BIND", "SENTINEL_CONSOLE_ORIGIN"), original):
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        importlib.reload(app.config)
        importlib.reload(app.main)
