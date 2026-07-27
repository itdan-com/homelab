"""Capability-scope derivation — the proxy can't be lied to.

The ext_authz callout hands us the ORIGINAL request (path appended
after the configured prefix, body forwarded via bodyToExtAuth). The
tool a caller is exercising is therefore derived HERE, in the trust
anchor, from what the request actually does — never from a header the
caller controls:

    server  = first segment of the proxied path   (/echo/mcp → "echo")
    leaf    = JSON-RPC params.name for tools/call ({"name": "say"} → "say")
              "rpc.<method>" for anything else    (tools/list → "rpc.tools.list")
    tool    = "<server>.<leaf>"                   ("echo.say")

The composite closes both spoofing holes at once: a token granted for
echo.say passes neither a different tool on echo nor any tool on any
other server. Anything unparseable denies closed — an attacker gains
nothing by sending garbage except an audit entry.

Deliberately strict for the MVP: JSON-RPC batches are refused (one
call, one scope, one audit line), and non-tools/call methods (MCP
handshake traffic like initialize / tools/list) need their own
"<server>.rpc.*" grants. If that proves too chatty for humans once
real MCP servers land in Phase 6, the roadmap's capability profiles
(preset bundles, one tap) are the answer — not loosening this parser.
"""

import json
import re

# A single path/tool segment. Tighter than schemas.TOOL_PATTERN because
# each segment must also survive being joined with "." into the
# composite tool name.
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# JSON-RPC method names: the MCP spec uses [a-z]+ with "/" separators
# (tools/call, resources/read, initialize). "/" becomes "." in the leaf.
_METHOD = re.compile(r"^[A-Za-z0-9._-]{1,64}(/[A-Za-z0-9._-]{1,64}){0,3}$")


def derive_scope(original_path: str, body: bytes) -> tuple[str | None, str]:
    """(tool, "ok") when the request maps to exactly one capability;
    (None, deny-reason) otherwise. Reasons are stable strings — they
    land verbatim in the audit log and the 403 body."""
    segments = [s for s in original_path.split("/") if s]
    if not segments or not _SEGMENT.fullmatch(segments[0]):
        return None, "unmapped-path"
    server = segments[0]

    if not body:
        return None, "empty-body"
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None, "malformed-body"
    if isinstance(doc, list):
        return None, "batch-unsupported"
    if not isinstance(doc, dict):
        return None, "malformed-body"

    method = doc.get("method")
    if not isinstance(method, str) or not _METHOD.fullmatch(method):
        return None, "malformed-body"

    if method == "tools/call":
        params = doc.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or not _SEGMENT.fullmatch(name):
            return None, "invalid-tool-name"
        leaf = name
    else:
        leaf = "rpc." + method.replace("/", ".")

    return f"{server}.{leaf}", "ok"
