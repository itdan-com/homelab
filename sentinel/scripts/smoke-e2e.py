#!/usr/bin/env python3
"""Phase 5.5.8 — the end-to-end battery.

Drives the REAL MCP protocol (official SDK client) through the real
Sentinel proxy against a real MCP server, and asserts the things the
phase doc asks for plus two the phase doc does not:

  * every layer refuses independently — a tool the human GRANTED is
    still refused by the MCP server's own allowlist
  * how many separate human approvals one honest MCP session costs

That count is the point. If it is four or five, the one-grant-per-call
model is unusable for real work and humans will rubber-stamp, which is
worse than no gate — and that finding is what Phase 6's policy work
(capability profiles, trust gradients) has to answer.

Self-authenticating: it enrols a throwaway software passkey, signs in,
and deletes the credential afterwards. That is not a backdoor — it is
exactly equivalent to host root, which already implies the ability to
enrol. It runs on the Sentinel host by design.
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from app.scope import derive_scope  # noqa: E402

ADMIN = os.environ.get("SENTINEL_ADMIN_URL", "http://127.0.0.1:8400")
BROKER = os.environ.get("SENTINEL_BROKER_URL")           # https://<gw-ip>:8401
CA = os.environ.get("SENTINEL_CERT_DIR", str(ROOT / "certs")) + "/ca.crt"
CLIENT_CRT = os.environ.get("SENTINEL_CERT_DIR", str(ROOT / "certs")) + "/proxy-client.crt"
CLIENT_KEY = os.environ.get("SENTINEL_CERT_DIR", str(ROOT / "certs")) + "/proxy-client.key"
PROXY = os.environ.get("SENTINEL_PROXY_URL", "http://127.0.0.1:18080")
MCP_PATH = "/mock/mcp"
FLOW = os.environ.get("SMOKE_FLOW", "smoke-" + str(int(time.time())))

PASS, FAIL = [], []


def check(ok: bool, what: str, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(what)
    print(f"  {'PASS' if ok else 'FAIL'}  {what}{('  — ' + detail) if detail else ''}")
    return ok


# --- talking to Sentinel ------------------------------------------------------

class Console:
    """The human's side, driven programmatically."""

    def __init__(self):
        self.cookies: dict[str, str] = {}

    def call(self, path, body=None, method=None):
        req = urllib.request.Request(
            ADMIN + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"content-type": "application/json",
                     "x-sentinel-console": "1",
                     **({"cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
                        if self.cookies else {})},
            method=method or ("POST" if body is not None else "GET"))
        try:
            r = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        for header in r.headers.get_all("set-cookie") or []:
            k, _, v = header.split(";")[0].partition("=")
            self.cookies[k] = v
        return r.status, json.loads(r.read() or b"{}")

    def sign_in(self, code: str | None = None):
        """Root mints its own enrolment code; anyone else passes one in
        with --enroll-code (from `sudo scripts/enroll-operator.sh`).
        Either way the gate holds: minting requires host privilege."""
        from authkit import SoftAuthenticator, challenge_of
        if code is None:
            from app.auth import mint_enrollment_code
            from app.db import SessionLocal
            with SessionLocal() as s:
                code = mint_enrollment_code(s, "smoke-operator", "smoke battery")
        device = SoftAuthenticator()
        _, started = self.call("/auth/register/begin", {"code": code})
        ch = challenge_of(started["options"])
        self.call("/auth/register/complete",
                  {"_challenge": ch, "credential": device.create(ch)})
        _, opts = self.call("/auth/login/begin", {})
        ch = challenge_of(opts["options"])
        status, who = self.call("/auth/login/complete",
                                {"_challenge": ch, "credential": device.get(ch)})
        return status == 200 and who.get("operator") == "smoke-operator"

    def cleanup(self):
        """Remove the throwaway operator. The audit trail stays — it
        should: the record must show that this ran. Needs DB access, so
        it is best-effort when the battery runs unprivileged."""
        try:
            self._cleanup()
        except Exception as exc:                          # noqa: BLE001
            print(f"  note: could not remove the throwaway operator ({exc}); "
                  "delete it from the console if you care")

    def _cleanup(self):
        from sqlalchemy import select
        from app.db import SessionLocal
        from app.models import ConsoleSession, Operator, WebAuthnCredential
        with SessionLocal() as s:
            op = s.scalars(select(Operator).where(
                Operator.username == "smoke-operator")).first()
            if op is None:
                return
            for model in (WebAuthnCredential, ConsoleSession):
                for row in s.scalars(select(model).where(
                        model.operator_id == op.id)).all():
                    s.delete(row)
            s.delete(op)
            s.commit()


def broker_call(path, body=None, headers=None):
    """The cluster-facing listener, over mTLS with the proxy's identity."""
    import ssl
    ctx = ssl.create_default_context(cafile=CA)
    ctx.load_cert_chain(CLIENT_CRT, CLIENT_KEY)
    req = urllib.request.Request(
        BROKER + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json", **(headers or {})},
        method="POST" if body is not None else "GET")
    try:
        r = urllib.request.urlopen(req, timeout=15, context=ctx)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    return r.status, json.loads(r.read() or b"{}")


def obtain(console, tool, nonce, ttl=5, reason=None):
    """The full loop for one capability: ask, human grants, claim."""
    _, req = broker_call("/v1/capability-requests", {
        "flow_id": FLOW, "tool": tool, "agent": "smoke-battery",
        "reason": reason or f"5.5.8 battery needs {tool}", "claim_nonce": nonce})
    rid = req["request_id"]
    status, _ = console.call(f"/v1/capability-requests/{rid}/grant",
                             {"ttl_minutes": ttl})
    if status != 201:
        return None
    _, polled = broker_call(f"/v1/capability-requests/{rid}",
                            headers={"X-Claim-Nonce": nonce})
    return polled.get("token")


# --- the MCP session ----------------------------------------------------------

def token_injecting_factory(tokens: dict[str, str], seen: list):
    """Every request in one MCP session needs a DIFFERENT token, because
    each carries a different scope — initialize, tools/list, the SSE
    channel and each tool call are four separate capabilities. Headers
    are per-connection in the SDK, so the token is selected per request
    here, using the broker's OWN derivation function so the two cannot
    silently disagree."""
    def factory(headers=None, timeout=None, auth=None):
        async def pick(request):
            body = request.content or b""
            tool, _ = derive_scope(request.method, MCP_PATH, body)
            seen.append(tool)
            if tool and tool in tokens:
                request.headers["X-Sentinel-Token"] = tokens[tool]
            request.headers["X-Flow-Id"] = FLOW
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                 follow_redirects=True,
                                 event_hooks={"request": [pick]})
    return factory


async def run_session(tokens, seen):
    async with streamablehttp_client(
            PROXY + MCP_PATH, timeout=20,
            httpx_client_factory=token_injecting_factory(tokens, seen)) as (r, w, _):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            said = await session.call_tool("say", {"message": "sentinel works"})
            try:
                refused = await session.call_tool("delete_everything", {})
            except Exception as exc:                      # noqa: BLE001
                refused = exc
            return init, tools, said, refused


# --- the battery --------------------------------------------------------------

def main() -> int:
    if not BROKER:
        print("!! set SENTINEL_BROKER_URL (https://<k3d-gateway-ip>:8401)")
        return 2
    print(f"\n== Sentinel end-to-end battery   flow={FLOW}\n")
    console = Console()

    print("-- the console is shut until a human authenticates")
    check(console.call("/v1/capability-requests")[0] == 401,
          "console refuses an unauthenticated caller")
    enroll_code = None
    if "--enroll-code" in sys.argv:
        enroll_code = sys.argv[sys.argv.index("--enroll-code") + 1]
    if not check(console.sign_in(enroll_code), "signed in with a passkey"):
        return 1

    # 1. No token at all.
    print("\n-- 1. no token")
    r = httpx.post(PROXY + MCP_PATH, timeout=15,
                   headers={"content-type": "application/json"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check(r.status_code == 403 and r.json().get("reason") == "missing-token",
          "proxy refuses a request with no capability", r.text.strip()[:60])

    # 2. How many approvals does ONE honest MCP session cost?
    print("\n-- 2. a real MCP session, one grant at a time")
    scopes = ["mock.rpc.initialize", "mock.rpc.notifications.initialized",
              "mock.rpc.tools.list", "mock.say", "mock.rpc.transport.get",
              "mock.rpc.transport.delete", "mock.delete_everything"]
    tokens, nonce_base = {}, "smoke-nonce-" + FLOW
    for i, scope in enumerate(scopes):
        tok = obtain(console, scope, f"{nonce_base}-{i}", ttl=5)
        if tok:
            tokens[scope] = tok
    check(len(tokens) == len(scopes),
          f"human approved {len(tokens)} separate capabilities for one session")

    seen: list = []
    try:
        init, tools, said, refused = asyncio.run(run_session(tokens, seen))
    except Exception as exc:                              # noqa: BLE001
        check(False, "MCP session completed through the proxy", repr(exc)[:160])
        init = tools = said = refused = None

    if init is not None:
        check(init.serverInfo.name == "mock-mcp",
              "initialize succeeded through the proxy", init.serverInfo.name)
        listed = [t.name for t in tools.tools]
        check(listed == ["say"],
              "tools/list shows only allowlisted tools", str(listed))
        text = said.content[0].text if said and said.content else ""
        check("sentinel works" in text, "tools/call ran the granted tool", text[:60])

        # 3. Layer independence: Sentinel GRANTED this one.
        print("\n-- 3. the layers refuse independently")
        # The SDK raises McpError for a JSON-RPC error, so the refusal
        # arrives as an exception rather than a result. Either shape is
        # the allowlist doing its job.
        detail = str(getattr(refused, "content", refused))[:160]
        check("allowlist" in detail or getattr(refused, "isError", False),
              "a tool the human granted is still refused by the server's allowlist",
              detail)

    used = [s for s in seen if s]
    print(f"\n  scopes the session actually exercised: {sorted(set(used))}")

    # 4. Wrong scope, wrong flow.
    print("\n-- 4. scope is locked")
    any_token = tokens.get("mock.say")
    r = httpx.post(PROXY + MCP_PATH, timeout=15,
                   headers={"content-type": "application/json",
                            "X-Sentinel-Token": any_token, "X-Flow-Id": FLOW},
                   json={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                         "params": {"name": "delete_everything"}})
    check(r.status_code == 403 and r.json().get("reason") == "scope-mismatch",
          "a token for one tool cannot invoke another")
    r = httpx.post(PROXY + MCP_PATH, timeout=15,
                   headers={"content-type": "application/json",
                            "X-Sentinel-Token": any_token, "X-Flow-Id": "some-other-flow"},
                   json={"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                         "params": {"name": "say", "arguments": {"message": "x"}}})
    check(r.status_code == 403 and r.json().get("reason") == "scope-mismatch",
          "a token cannot be replayed from another flow")

    # 5. The kill switch, through the real proxy.
    print("\n-- 5. the kill switch")
    status, killed = console.call("/v1/kill", {"reason": "5.5.8 battery"})
    check(status == 200 and killed.get("grants_revoked", 0) >= 1,
          f"kill revoked {killed.get('grants_revoked')} live grants")
    r = httpx.post(PROXY + MCP_PATH, timeout=15,
                   headers={"content-type": "application/json",
                            "X-Sentinel-Token": any_token, "X-Flow-Id": FLOW},
                   json={"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                         "params": {"name": "say", "arguments": {"message": "x"}}})
    check(r.status_code == 403 and r.json().get("reason") == "kill-engaged",
          "every capability dies immediately, mid-session")
    console.call("/v1/kill/release", {}, method="POST")
    r = httpx.post(PROXY + MCP_PATH, timeout=15,
                   headers={"content-type": "application/json",
                            "X-Sentinel-Token": any_token, "X-Flow-Id": FLOW},
                   json={"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                         "params": {"name": "say", "arguments": {"message": "x"}}})
    check(r.status_code == 403 and r.json().get("reason") == "revoked",
          "release does not resurrect what kill revoked")

    # 6. TTL expiry — the one assertion that costs real time.
    if os.environ.get("SMOKE_SKIP_TTL"):
        print("\n-- 6. TTL expiry (skipped: SMOKE_SKIP_TTL)")
    else:
        print("\n-- 6. TTL expiry (waiting ~65s; the minimum grant is 1 minute)")
        short = obtain(console, "mock.say", nonce_base + "-ttl", ttl=1,
                       reason="TTL expiry assertion")
        r = httpx.post(PROXY + MCP_PATH, timeout=15,
                       headers={"content-type": "application/json",
                                "X-Sentinel-Token": short, "X-Flow-Id": FLOW},
                       json={"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                             "params": {"name": "say", "arguments": {"message": "x"}}})
        check(r.status_code == 200, "the short-lived grant works while it lives")
        time.sleep(65)
        r = httpx.post(PROXY + MCP_PATH, timeout=15,
                       headers={"content-type": "application/json",
                                "X-Sentinel-Token": short, "X-Flow-Id": FLOW},
                       json={"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                             "params": {"name": "say", "arguments": {"message": "x"}}})
        check(r.status_code == 403 and r.json().get("reason") == "expired",
              "and stops working the moment it expires")

    # 7. The record.
    print("\n-- 7. the canonical record")
    _, events = console.call(f"/v1/audit-events?limit=500&flow_id={FLOW}")
    kinds = {e["event_type"] for e in events}
    check(len(kinds) >= 5, f"audit captured {len(kinds)} event types: {sorted(kinds)}")

    console.cleanup()
    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"   FAILED: {f}")
    print(f"\n== HEADLINE: one MCP session required {len(tokens)} human approvals.\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
