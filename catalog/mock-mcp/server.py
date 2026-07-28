"""A real MCP server, small enough to read in one sitting.

It exists to prove Sentinel's enforcement chain against the ACTUAL MCP
wire protocol rather than a stand-in that only ever answers POSTs.
Everything the proxy has to cope with is here: Streamable HTTP, the
bodiless GET that opens the server-push channel, the bodiless DELETE
that ends a session, and session ids.

Deliberately Python standard library only. It ships as a ConfigMap on a
stock `python:3.12-slim`, so there is no image to build and nothing to
`pip install` at pod start — a test fixture that needs the network to
boot is a test fixture that fails for reasons unrelated to the test.
The arbiter of "is this really MCP" is the official SDK client the
smoke battery drives it with; if that completes a session, the protocol
is right.

THE TOOL ALLOWLIST IS THE POINT (CLAUDE.md, defence in depth): every
MCP server ships its own allowlist "even if a Sentinel grant
accidentally matched a more powerful tool, the MCP server itself would
refuse". `delete_everything` below is fully implemented and omitted
from the allowlist precisely so that claim can be tested rather than
asserted — grant it at Sentinel, present a valid token, and it must
still fail here.
"""

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MCP_PORT", "8080"))
SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "mock-mcp")
# Layer 2. Tools outside this list are refused even when a caller
# arrives with a perfectly valid, human-granted Sentinel capability.
ALLOWED = {t.strip() for t in os.environ.get("MCP_ALLOWED_TOOLS", "say").split(",") if t.strip()}

TOOLS = {
    "say": {
        "description": "Echo a message back. Harmless on purpose.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    "delete_everything": {
        "description": "Implemented, and deliberately NOT allowlisted — the "
                       "defence-in-depth test calls this with a valid grant.",
        "inputSchema": {"type": "object", "properties": {}},
    },
}

_sessions: set[str] = set()
_lock = threading.Lock()


def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def handle(message: dict) -> dict | None:
    """Returns a JSON-RPC response, or None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _result(request_id, {
            # Echo the client's version: this is a fixture, not a
            # negotiation testbed, and mismatches would only obscure
            # what we are actually measuring.
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        # Non-allowlisted tools are not advertised AND not callable.
        return _result(request_id, {"tools": [
            {"name": name, **spec} for name, spec in TOOLS.items() if name in ALLOWED
        ]})

    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return _error(request_id, -32602, f"unknown tool: {name}")
        if name not in ALLOWED:
            # The whole reason this server exists in this shape.
            return _error(
                request_id, -32001,
                f"tool '{name}' is not in this server's allowlist. Sentinel may "
                "have granted it; this server still refuses. Three independent "
                "layers must agree.")
        if name == "say":
            text = (params.get("arguments") or {}).get("message", "hello")
            return _result(request_id, {
                "content": [{"type": "text", "text": f"{SERVER_NAME} says: {text}"}],
                "isError": False,
            })
        return _result(request_id, {"content": [], "isError": False})

    return _error(request_id, -32601, f"method not found: {method}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # one line per request, to stdout
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def _send(self, code, body=None, headers=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            message = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": "malformed json"})
            return

        extra = {}
        if message.get("method") == "initialize":
            session = uuid.uuid4().hex
            with _lock:
                _sessions.add(session)
            extra["Mcp-Session-Id"] = session

        response = handle(message)
        if response is None:
            self._send(202, None, extra)     # notification: accepted, no body
        else:
            self._send(200, response, extra)

    def do_GET(self):
        """The server-push channel. A bodiless GET — one of the two verbs
        that had never met a real client before this test existed."""
        if "text/event-stream" not in (self.headers.get("Accept") or ""):
            self._send(405, {"error": "this endpoint is the SSE channel"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # A comment frame opens the stream; the client keeps it in a
            # background task. Nothing is pushed — this fixture only has
            # to prove the channel can be opened through the proxy.
            self.wfile.write(b": mcp stream open\n\n")
            self.wfile.flush()
            while True:
                if self.wfile.closed:
                    break
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                threading.Event().wait(15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_DELETE(self):
        """Session termination. The other bodiless verb."""
        session = self.headers.get("Mcp-Session-Id")
        with _lock:
            _sessions.discard(session)
        self._send(204)


if __name__ == "__main__":
    print(f"{SERVER_NAME} listening on :{PORT}; allowlist={sorted(ALLOWED)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
