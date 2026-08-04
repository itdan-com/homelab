"""Upstream credentials for MCP servers (7.4).

Every MCP server behind Airlock needs some credential at its own
upstream — a GitHub token, a Slack `xoxb`, a database password. Two
rules shape this module:

**The workload never holds it.** GitHub's MCP server has no static
token mode over HTTP at all: every request must carry its own
`Authorization` header. So the credential belongs to the component
that authorizes the call — Sentinel — and the pod holds nothing.
Compromising an MCP server steals no credential because there isn't
one in it.

**Nobody rotates anything by hand.** A personal access token expires,
and a human re-pasting one every 90 days is an outage with a calendar
invite. The production shape is a **GitHub App**: the private key does
not expire, and *installation tokens* are minted from it on demand and
live one hour. This module does that minting — the same dance
`ops/operator/bin/gh-app-token.sh` already does for Mission Control,
moved in-process and cached.

Airlock uses its OWN App, never Mission Control's. Sharing one would
collapse attribution between the two flows: every action would read as
"the operator" whether the platform proposed it or a person did.

Config lives in a root-owned JSON file the service can read and cannot
write (`/etc/sentinel/upstream-tokens.json`), one entry per server:

    {
      "github": {"app_id": "12345",
                 "private_key_file": "/etc/sentinel/airlock-github-app.pem",
                 "installation_id": "67890"},
      "slack":  {"token": "xoxb-..."}
    }

A bare string is accepted as shorthand for `{"token": "..."}`, so a
static credential stays a one-liner.
"""

import json
import os
import logging
import threading
import time
from pathlib import Path

import httpx
import jwt

log = logging.getLogger("sentinel")

# Refresh this long before GitHub's stated expiry. An hour-long token
# refreshed at 55 minutes never becomes a mid-call failure.
REFRESH_MARGIN_SECONDS = 300
JWT_LIFETIME_SECONDS = 540   # GitHub rejects App JWTs older than 10 min

_cache: dict[str, tuple[str, float]] = {}   # server -> (token, expires_at)
_lock = threading.Lock()


class UpstreamAuthError(Exception):
    """No usable credential. The caller turns this into a refusal that
    names the server, never into a call without a credential."""


def _load(path: str) -> dict:
    try:
        doc = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise UpstreamAuthError(f"credential file unreadable: {e}")
    return doc if isinstance(doc, dict) else {}


def _app_jwt(app_id: str, key_path: str) -> str:
    """A short-lived assertion signed with the App's private key. `iat`
    is backdated a minute because GitHub rejects tokens whose issue
    time is in its future — clock skew between two machines is normal
    and must not look like an attack."""
    now = int(time.time())
    try:
        key = Path(key_path).read_text()
    except OSError as e:
        raise UpstreamAuthError(f"App private key unreadable: {e}")
    return jwt.encode({"iat": now - 60, "exp": now + JWT_LIFETIME_SECONDS,
                       "iss": str(app_id)}, key, algorithm="RS256")


def _mint_installation_token(cfg: dict) -> tuple[str, float]:
    """Exchange the App assertion for an installation token. Returns
    (token, unix expiry). GitHub's own expiry is authoritative — we do
    not assume the documented hour, because assuming a vendor's TTL is
    how a credential expires in production and not in testing."""
    api = (cfg.get("api_base") or "https://api.github.com").rstrip("/")
    assertion = _app_jwt(cfg["app_id"], cfg["private_key_file"])
    headers = {"Authorization": f"Bearer {assertion}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    with httpx.Client(timeout=10.0) as c:
        installation = cfg.get("installation_id")
        if not installation:
            # Discover it once. An App installed on exactly one account
            # is the normal case; more than one is ambiguous and the
            # operator must say which, rather than us guessing.
            r = c.get(f"{api}/app/installations", headers=headers)
            if r.status_code != 200:
                raise UpstreamAuthError(
                    f"listing App installations failed ({r.status_code})")
            found = r.json()
            if len(found) != 1:
                raise UpstreamAuthError(
                    f"App has {len(found)} installations — set "
                    f"installation_id explicitly")
            installation = found[0]["id"]
        r = c.post(f"{api}/app/installations/{installation}/access_tokens",
                   headers=headers)
    if r.status_code != 201:
        raise UpstreamAuthError(
            f"minting an installation token failed ({r.status_code})")
    body = r.json()
    expires = body.get("expires_at")
    try:
        from datetime import datetime
        exp = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
    except Exception:
        exp = time.time() + 3600
    return body["token"], exp


def token_for(server: str, path: str) -> str | None:
    """The credential to present upstream for `server`, or None when
    the deployment has not configured one (a decidable server that
    cannot be called — an honest state, not an error).

    App-backed entries mint on first use and re-mint before expiry, so
    nothing on this path ever depends on a human remembering."""
    entry = _load(path).get(server)
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry or None
    if entry.get("token"):
        return entry["token"]
    if not entry.get("app_id") or not entry.get("private_key_file"):
        raise UpstreamAuthError(
            f"{server}: needs either `token`, or `app_id` + "
            f"`private_key_file`")

    with _lock:
        cached = _cache.get(server)
        if cached and cached[1] - REFRESH_MARGIN_SECONDS > time.time():
            return cached[0]
        token, exp = _mint_installation_token(entry)
        _cache[server] = (token, exp)
        log.info("minted a %s installation token, valid %d minutes",
                 server, int((exp - time.time()) / 60))
        return token


def forget(server: str | None = None) -> None:
    """Drop cached tokens — used by tests and by a credential rotation
    that should take effect now rather than at the next refresh."""
    with _lock:
        _cache.clear() if server is None else _cache.pop(server, None)


# --- console management (7.4) -------------------------------------------------
#
# Credentials are pasted into the console, not edited on a host over
# SSH. Same gate as every other console write — a passkey holder, an
# audit row — and the same reasoning as the policy store: this is
# operational data the operator owns, not the unit's own configuration.

def _key_fingerprint(pem: str) -> str:
    """A short, non-secret identifier for a private key, so the console
    can show WHICH key is installed without ever showing the key."""
    import hashlib
    from cryptography.hazmat.primitives import serialization as _ser
    k = _ser.load_pem_private_key(pem.encode(), password=None)
    pub = k.public_key().public_bytes(
        _ser.Encoding.DER, _ser.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(pub).hexdigest()[:16]


def describe(path: str) -> list[dict]:
    """What the console shows: which servers have a credential, of what
    kind, and enough identity to recognise it — never the secret."""
    out = []
    for server, entry in sorted(_load(path).items()):
        if isinstance(entry, str) or entry.get("token"):
            where = entry.get("url", "") if isinstance(entry, dict) else ""
            out.append({"server": server, "kind": "token",
                        "detail": (f"{where} · " if where else "") + "static token"})
            continue
        detail = f"App {entry.get('app_id', '?')}"
        if entry.get("url"):
            detail = f"{entry['url']} · " + detail
        if entry.get("installation_id"):
            detail += f", installation {entry['installation_id']}"
        if entry.get("key_fingerprint"):
            detail += f", key {entry['key_fingerprint']}"
        cached = _cache.get(server)
        if cached:
            mins = int((cached[1] - time.time()) / 60)
            detail += f" · token valid {mins} more minutes"
        out.append({"server": server, "kind": "app", "detail": detail})
    return out


def save(path: str, server: str, entry: dict) -> dict:
    """Validate then write. A private key that does not parse is
    rejected HERE, with a message, rather than becoming a 500 at the
    first tool call an hour later."""
    if not server or not server.replace("-", "").replace("_", "").isalnum():
        raise UpstreamAuthError("server name must be alphanumeric")
    doc = _load(path)
    url = (entry.get("url") or "").strip()
    if url and not url.startswith("https://") and not url.startswith("http://"):
        raise UpstreamAuthError("the address must be a URL")
    if entry.get("token"):
        doc[server] = {"token": entry["token"].strip()}
    elif entry.get("private_key"):
        pem = entry["private_key"].strip() + "\n"
        if not entry.get("app_id", "").strip().isdigit():
            raise UpstreamAuthError("App ID must be the number GitHub shows")
        try:
            fp = _key_fingerprint(pem)
        except Exception as e:
            raise UpstreamAuthError(f"that private key could not be read: {e}")
        key_path = Path(path).with_name(f"{server}-app-key.pem")
        # 0600 before any bytes are written — a key file that was
        # briefly world-readable was briefly compromised.
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        doc[server] = {"app_id": entry["app_id"].strip(),
                       "private_key_file": str(key_path),
                       "key_fingerprint": fp}
        if entry.get("installation_id", "").strip():
            doc[server]["installation_id"] = entry["installation_id"].strip()
    else:
        raise UpstreamAuthError("provide either a token, or an App ID and key")
    # WHERE the server is lives beside HOW we authenticate to it, so
    # choosing GitHub-hosted over self-hosted is a field on this form
    # rather than an env file on a host. Policy is unaffected either
    # way: the gate, the audit log and the elevation windows sit in
    # front of the address, not behind it.
    if url:
        doc[server]["url"] = url

    tmp = Path(path).with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1)
    tmp.replace(path)          # atomic: never a half-written credential file
    forget(server)             # a new credential takes effect on the next call
    return doc[server]


def remove(path: str, server: str) -> bool:
    doc = _load(path)
    if server not in doc:
        return False
    doc.pop(server)
    tmp = Path(path).with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1)
    tmp.replace(path)
    forget(server)
    return True


def upstream_url(server: str, path: str) -> str | None:
    """Where this server lives, if the connection says. Falls back to
    the deployment's static config, so an existing install keeps
    working unchanged."""
    entry = _load(path).get(server)
    return entry.get("url") if isinstance(entry, dict) else None


def is_registered(server: str, path: str) -> bool:
    """Has an operator connected this server at all? Distinguishes
    "runs here, address derived" from "nothing configured"."""
    return server in _load(path)


def discover_tools(server: str, url: str, token: str | None,
                   ca_bundle: str | None = None,
                   gate_headers: dict | None = None) -> dict:
    """Ask a server what it can do, and classify it from what it says.

    MCP tools carry `annotations.readOnlyHint` and `destructiveHint`,
    so a server describes its own verbs and the platform does not have
    to be told them by a human retyping a list. Returns
    {read: [...], write: [...], destructive: [...]}.

    `destructive` is returned SEPARATELY and deliberately left out of
    both classified sets: an unclassified tool is denied, so a
    destructive verb a server offers cannot be called until a human
    decides it should be. Discovery proposes; it never widens access on
    its own.
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Discovery goes through the enforcement proxy like every other
    # call — an admin action is not an exemption from the gate, it just
    # carries a system-issued capability instead of a person's.
    headers.update(gate_headers or {})
    with httpx.Client(timeout=15.0, verify=ca_bundle or True) as c:
        r = c.post(url, json=body, headers=headers)
    if r.status_code != 200:
        raise UpstreamAuthError(f"{server} did not answer tools/list "
                                f"(HTTP {r.status_code})")
    # Check the CONTENT TYPE first. An MCP server may answer a POST as
    # JSON or as a one-message event stream — its choice, not ours — and
    # parsing before looking threw on the stream form.
    if "text/event-stream" in r.headers.get("content-type", ""):
        payload = {}
        for line in r.text.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                break
    else:
        payload = r.json()
    tools = (payload.get("result") or {}).get("tools")
    if tools is None:
        raise UpstreamAuthError(f"{server} returned no tool list")

    read, write, destructive = [], [], []
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        ann = t.get("annotations") or {}
        # `readOnlyHint` is the primary signal, and `destructiveHint`
        # is only consulted for tools that are NOT read-only.
        #
        # Not a style choice: MCP's spec default for destructiveHint is
        # TRUE, so a server that annotates it selectively leaves it set
        # on everything it did not think about. Slack's server does
        # exactly that — 21 of its 22 tools report destructive, read-only
        # ones included — and reading destructive first would have
        # classified its whole catalog as dangerous, left every tool
        # unclassified, and made the server silently uncallable.
        # A default that means "unknown" must not be read as "yes".
        if ann.get("readOnlyHint"):
            read.append(name)
        elif ann.get("destructiveHint"):
            destructive.append(name)
        else:
            # No hint at all, or explicitly not read-only: a write. The
            # safe direction — write is the more restricted rung, and a
            # tool nobody annotated is a tool nobody vouched for.
            write.append(name)
    # The handshake always rides a prefix class, or an assigned server
    # could not even be initialized.
    read.append("rpc.*")
    return {"read": sorted(set(read)), "write": sorted(set(write)),
            "destructive": sorted(set(destructive))}
