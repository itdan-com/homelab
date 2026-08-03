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
