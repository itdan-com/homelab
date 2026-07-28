#!/usr/bin/env python3
"""Make the console show something real.

Until the control-plane Claude exists (Phase 6), nothing ever asks
Sentinel for anything, so the console is an empty room with a kill
switch in it. That is not a bug — there is simply no agent yet — but it
does mean a human has no way to SEE the thing work, or to screenshot it.

This raises one realistic capability request and waits for you to answer
it in the console, then completes the loop through the proxy so you can
watch the whole thing end to end:

    you run this  ->  a card appears in the console  ->  you click Grant
                  ->  the agent's call succeeds through the proxy

Runs as you, not root: it uses the repo's copy of the client cert, which
is the same identity the in-cluster proxy holds.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CERTS = Path(os.environ.get("SENTINEL_CERT_DIR", ROOT / "certs"))
BROKER = os.environ["SENTINEL_BROKER_URL"]
PROXY = os.environ.get("SENTINEL_PROXY_URL", "http://127.0.0.1:18080")
CONSOLE = os.environ.get("SENTINEL_CONSOLE_ORIGIN", "http://localhost:8400")
FLOW = "demo-" + str(int(time.time()))
NONCE = "demo-nonce-" + FLOW

# A reason written for the human who will read it, because that is the
# entire point of the approval screen.
REASON = ("Call the mock MCP server's `say` tool to prove the capability "
          "loop works end to end. Harmless — it echoes a string back.")


def broker(path, body=None, headers=None):
    ctx = ssl.create_default_context(cafile=str(CERTS / "ca.crt"))
    ctx.load_cert_chain(str(CERTS / "proxy-client.crt"), str(CERTS / "proxy-client.key"))
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


def main() -> int:
    _, made = broker("/v1/capability-requests", {
        "flow_id": FLOW, "tool": "mock.say", "agent": "demo-agent",
        "reason": REASON, "claim_nonce": NONCE})
    rid = made.get("request_id")
    if not rid:
        print(f"!! the broker refused the request: {made}")
        return 1

    print(f"""
  A capability request is now waiting for you.

      open:  {CONSOLE}
      flow:  {FLOW}
      tool:  mock.say

  You should see a card naming the agent, the tool and the reason, with
  Grant 5m / Grant 1h / Deny. **This is the screenshot.**

  Waiting for your answer (Ctrl-C to give up)...""")

    deadline = time.time() + 600
    while time.time() < deadline:
        _, polled = broker(f"/v1/capability-requests/{rid}",
                           headers={"X-Claim-Nonce": NONCE})
        status = polled.get("status")
        if status == "granted":
            token = polled.get("token")
            print("\n  Granted. Using the capability now...")
            break
        if status in ("denied", "expired"):
            print(f"\n  {status.upper()} — the agent's action fails closed, "
                  "which is the correct outcome. Nothing happened.")
            return 0
        time.sleep(2)
    else:
        print("\n  timed out waiting for an answer")
        return 1

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "say",
                                  "arguments": {"message": "the gate works"}}}).encode()
    req = urllib.request.Request(
        PROXY + "/mock/mcp", data=body, method="POST",
        headers={"content-type": "application/json",
                 "accept": "application/json, text/event-stream",
                 "X-Sentinel-Token": token, "X-Flow-Id": FLOW})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            answer = json.loads(r.read())
        text = answer["result"]["content"][0]["text"]
        print(f"  the MCP server answered: {text}")
    except urllib.error.HTTPError as e:
        print(f"  the proxy refused it: {e.code} {e.read().decode()[:120]}")
        return 1

    print(f"""
  That is the whole loop:

    the agent asked  ->  you decided  ->  Sentinel minted a token scoped
    to ONE tool and ONE flow for 5 minutes  ->  the proxy checked it on
    the way through  ->  the MCP server answered.

  Everything that just happened is on the record; the console's audit
  panel shows it, filtered to flow {FLOW}.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
