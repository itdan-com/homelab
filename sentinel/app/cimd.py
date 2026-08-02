"""Client identity for the door (7.3.3): CIMD documents and redirect
URI matching.

MCP's 2026-07-28 revision replaced dynamic client registration with
**Client ID Metadata Documents**: the client_id IS an https URL, and
the authorization server fetches it to learn the client's name and
redirect URIs. Claude Code sends
`https://claude.ai/oauth/claude-code-client-metadata` (observed
2026-08-02).

Two things here are load-bearing security, not plumbing:

**The fetch is server-side and attacker-influenced.** A client_id is
whatever the caller typed, so fetching it is a textbook SSRF primitive
pointed at our own host — which sits in Sentinel's trust domain beside
a loopback admin API. Hence: https only, no redirects followed, every
resolved address checked against private/loopback/link-local ranges
BEFORE connecting, a size cap, and a short timeout.

**Redirect matching follows RFC 8252 §7.3 for loopback.** A native app
must use an ephemeral loopback port, so the port is NOT part of the
comparison for `http://127.0.0.1` / `http://localhost` URIs — the rest
must match exactly. This is also what makes claude-code #37747 (a
CIMD document listing PORTLESS redirect URIs while the request carries
a real port) a non-issue here: we implement the spec rather than
loosening the allowlist with wildcards.
"""

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

MAX_DOC_BYTES = 64 * 1024
FETCH_TIMEOUT_SECONDS = 5.0
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ClientError(ValueError):
    """The client identity is unusable. The message is safe to log; it
    is never reflected into a redirect (an unvalidated redirect target
    is how open redirectors are born)."""


def _assert_public_address(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ClientError(f"client_id host does not resolve: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ClientError(
                f"client_id resolves to a non-public address ({ip}) — refused")


def fetch_cimd(client_id: str, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> dict:
    """Fetch and sanity-check a Client ID Metadata Document. Returns the
    parsed document; raises ClientError on anything suspicious."""
    u = urlparse(client_id)
    if u.scheme != "https" or not u.hostname:
        raise ClientError("client_id must be an https URL (CIMD) "
                          "or a statically registered id")
    if u.fragment or u.username or u.password:
        raise ClientError("client_id URL must carry no fragment or credentials")
    _assert_public_address(u.hostname)

    try:
        with httpx.Client(follow_redirects=False, timeout=timeout) as c:
            r = c.get(client_id, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise ClientError(f"client metadata unreachable: {e}")
    if r.status_code != 200:
        # A redirect is refused rather than followed: the redirect target
        # is chosen by the same party we are guarding against, so
        # following it would walk straight around the address check.
        raise ClientError(f"client metadata returned HTTP {r.status_code}")
    if len(r.content) > MAX_DOC_BYTES:
        raise ClientError("client metadata document too large")
    try:
        doc = r.json()
    except ValueError:
        raise ClientError("client metadata is not JSON")
    if not isinstance(doc, dict):
        raise ClientError("client metadata must be a JSON object")
    if doc.get("client_id") != client_id:
        # Binds the document to the identity it was fetched for;
        # otherwise one hosted document could claim to be any client.
        raise ClientError("client metadata client_id does not match the URL")
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(
            isinstance(x, str) for x in uris):
        raise ClientError("client metadata has no usable redirect_uris")
    return doc


def redirect_uri_allowed(candidate: str, registered: list[str]) -> bool:
    """RFC 8252 §7.3: for loopback http URIs the PORT is ignored and
    everything else must match exactly. For every other URI the match
    is byte-exact — no wildcards, no prefix matching, ever."""
    try:
        c = urlparse(candidate)
    except ValueError:
        return False
    if c.fragment:
        return False  # redirect URIs may not carry fragments
    for reg in registered:
        if candidate == reg:
            return True
        r = urlparse(reg)
        if (c.scheme == r.scheme == "http"
                and c.hostname in _LOOPBACK_HOSTS
                and r.hostname in _LOOPBACK_HOSTS
                and c.hostname == r.hostname
                and (c.path or "/") == (r.path or "/")
                and c.query == r.query):
            return True
    return False
