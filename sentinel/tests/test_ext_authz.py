"""The ext_authz entry point — every way the proxy's question gets
answered, plus the scope-derivation table it depends on.

The property under test is the one the whole proxy exists for: the
TOOL is derived from what the request actually does (path + JSON-RPC
body), never from anything the caller asserts — so a caller holding a
grant for echo.say cannot smuggle a call to anything else past the
proxy, on any server. Same in-process style as test_broker_flow.py;
flow ids are unique to this module so the two files share a DB safely
in one pytest run.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# First-imported test module binds the engine; setdefault keeps this
# file standalone-runnable without stomping a sibling's DB in-process.
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
from app.scope import derive_scope  # noqa: E402

_HERE = os.path.dirname(__file__)
broker = TestClient(broker_app)
# https + sign-in: since 5.5.6 the console has no anonymous
# surface, so authenticating first is simply what using it looks
# like (the session cookie is Secure, which an http client drops).
admin = TestClient(admin_app, base_url="https://testserver")

AUTHZ = "/v1/ext-authz"
CONSOLE = {"x-sentinel-console": "1"}  # the admin app's CSRF guard


def _migrate() -> None:
    cfg = Config(os.path.join(_HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_HERE, "..", "migrations"))
    command.upgrade(cfg, "head")


NONCE = "ext-authz-claim-nonce-0123456789"


def _granted_token(flow: str, tool: str) -> str:
    rid = broker.post("/v1/capability-requests",
                      json={"flow_id": flow, "tool": tool, "reason": "test",
                            "agent": "pytest", "claim_nonce": NONCE}
                      ).json()["request_id"]
    admin.post(f"/v1/capability-requests/{rid}/grant",
               json={"ttl_minutes": 5}, headers=CONSOLE)
    return broker.get(f"/v1/capability-requests/{rid}",
                      headers={"X-Claim-Nonce": NONCE}).json()["token"]


def _call(path="/echo/mcp", token=None, flow=None, json_body=None, content=None):
    headers = {}
    if token is not None:
        headers["X-Sentinel-Token"] = token
    if flow is not None:
        headers["X-Flow-Id"] = flow
    kwargs = {"headers": headers}
    if content is not None:
        kwargs["content"] = content
    else:
        kwargs["json"] = json_body
    return broker.post(AUTHZ + path, **kwargs)


def _tools_call(name):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": {}}}


def test_derive_scope_table():
    body = lambda name: __import__("json").dumps(_tools_call(name)).encode()
    assert derive_scope("POST", "/echo/mcp", body("say")) == ("echo.say", "ok")
    assert derive_scope("POST", "/github/mcp", body("create_pr")) == ("github.create_pr", "ok")
    # non-tools/call methods become <server>.rpc.<method with / → .>
    import json as _json
    rpc = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    assert derive_scope("POST", "/echo/mcp", rpc) == ("echo.rpc.tools.list", "ok")
    init = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
    assert derive_scope("POST", "/echo/mcp", init) == ("echo.rpc.initialize", "ok")
    # deny-closed branches
    assert derive_scope("POST", "", body("say")) == (None, "unmapped-path")
    assert derive_scope("POST", "/", body("say")) == (None, "unmapped-path")
    assert derive_scope("POST", "/bad seg/x", body("say")) == (None, "unmapped-path")
    assert derive_scope("POST", "/echo/mcp", b"") == (None, "empty-body")
    assert derive_scope("POST", "/echo/mcp", b"not json") == (None, "malformed-body")
    assert derive_scope("POST", "/echo/mcp", b"[{}]") == (None, "batch-unsupported")
    assert derive_scope("POST", "/echo/mcp", b'"str"') == (None, "malformed-body")
    assert derive_scope("POST", "/echo/mcp", b'{"method": 7}') == (None, "malformed-body")
    noname = _json.dumps({"method": "tools/call", "params": {}}).encode()
    assert derive_scope("POST", "/echo/mcp", noname) == (None, "invalid-tool-name")
    dotted = _json.dumps(_tools_call("../../etc")).encode()
    assert derive_scope("POST", "/echo/mcp", dotted) == (None, "invalid-tool-name")

    # MCP Streamable HTTP opens its push channel with a bodiless GET and
    # ends the session with a bodiless DELETE. These must be GRANTABLE,
    # not impossible — denying them outright meant no MCP session could
    # ever be established through the proxy.
    assert derive_scope("GET", "/echo/mcp", b"") == ("echo.rpc.transport.get", "ok")
    assert derive_scope("DELETE", "/echo/mcp", b"") == ("echo.rpc.transport.delete", "ok")
    # ...but a POST with no JSON-RPC document is still malformed.
    assert derive_scope("POST", "/echo/mcp", b"") == (None, "empty-body")

    # Authorization and execution must read the SAME document: with
    # duplicate keys, Python takes the last and a first-wins parser takes
    # the first, so Sentinel would authorize `say` while the upstream ran
    # `delete_repo`. Refuse rather than bet on the upstream's parser.
    dup = b'{"method":"tools/call","params":{"name":"delete_repo"},"params":{"name":"say"}}'
    assert derive_scope("POST", "/echo/mcp", dup) == (None, "malformed-body")
    assert derive_scope("POST", "/echo/mcp", b'{"method":"tools/call","params":NaN}') \
        == (None, "malformed-body")


def test_ext_authz_verdicts():
    _migrate()
    sign_in(admin)
    token = _granted_token("xz-flow-A", "echo.say")

    # happy path: 200, grant identity attached for the upstream's log
    r = _call(token=token, flow="xz-flow-A", json_body=_tools_call("say"))
    assert r.status_code == 200, "granted tool through the real body path"
    assert r.json()["tool"] == "echo.say"
    assert r.headers["x-sentinel-grant-id"] and r.headers["x-sentinel-tool"] == "echo.say"

    # THE property: same token, body names a different tool → refused
    r = _call(token=token, flow="xz-flow-A", json_body=_tools_call("delete_everything"))
    assert r.status_code == 403 and r.json()["reason"] == "scope-mismatch", \
        "a caller cannot invoke a tool it wasn't granted, whatever it claims"

    # ...and the same tool leaf on a different server → refused
    r = _call(path="/github/mcp", token=token, flow="xz-flow-A",
              json_body=_tools_call("say"))
    assert r.status_code == 403 and r.json()["reason"] == "scope-mismatch", \
        "composite server.tool scope blocks cross-server reuse"

    # pre-check refusals, each with its own audited reason
    assert _call(flow="xz-flow-A", json_body=_tools_call("say")).json()["reason"] \
        == "missing-token"
    assert _call(token=token, json_body=_tools_call("say")).json()["reason"] \
        == "missing-flow-id"
    assert _call(token=token, flow="bad flow!", json_body=_tools_call("say")
                 ).json()["reason"] == "invalid-flow-id"
    assert _call(token=token, flow="xz-flow-A", content=b"not json"
                 ).json()["reason"] == "malformed-body"
    assert _call(token=token, flow="xz-flow-A", content=b"[]"
                 ).json()["reason"] == "batch-unsupported"
    assert _call(path="", token=token, flow="xz-flow-A",
                 json_body=_tools_call("say")).json()["reason"] == "unmapped-path"

    # unknown token is still the broker's call, not ours
    assert _call(token="snt_forged", flow="xz-flow-A",
                 json_body=_tools_call("say")).json()["reason"] == "unknown-token"

    # rpc-method derivation end-to-end: tools/list needs its own grant
    rpc_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    r = _call(token=token, flow="xz-flow-A", json_body=rpc_body)
    assert r.status_code == 403, "handshake traffic is not covered by a tool grant"
    rpc_token = _granted_token("xz-flow-A", "echo.rpc.tools.list")
    r = _call(token=rpc_token, flow="xz-flow-A", json_body=rpc_body)
    assert r.status_code == 200 and r.json()["tool"] == "echo.rpc.tools.list"

    # every refusal above left a trail: audit shows ext-authz-sourced denials
    events = admin.get("/v1/audit-events",
                       params={"event_type": "denial", "limit": 500}).json()
    sources = {e["details"].get("source") for e in events if e["details"]}
    assert "ext-authz" in sources, "pre-check denials are audited"
    assert any(e["details"].get("reason") == "missing-token" for e in events
               if e["details"] and e["details"].get("source") == "ext-authz")


if __name__ == "__main__":
    test_derive_scope_table()
    test_ext_authz_verdicts()
    print("ok")
