"""7.3.3: the door — discovery documents, CIMD client identity with its
SSRF guard, RFC 8252 loopback redirect matching, PKCE enforcement, the
Authentik federation leg (stubbed at the HTTP boundary), single-use
codes, and resource-bound person-tokens.

    python -m pytest tests/test_door.py -q
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_TMP = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(_TMP, "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")
os.environ.setdefault("SENTINEL_DOOR_KEY", os.path.join(_TMP, "door-key.pem"))
os.environ.setdefault("SENTINEL_DOOR_ORIGIN", "https://mcp.test.local")
os.environ.setdefault("SENTINEL_POLICY_RELOAD_SECONDS", "0")

import jwt  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import cimd, door  # noqa: E402
from app.cimd import ClientError, redirect_uri_allowed  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Principal  # noqa: E402


def _migrate():
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location",
                        str(Path(__file__).resolve().parents[1] / "migrations"))
    command.upgrade(cfg, "head")


_migrate()

CLIENT = "https://client.example/meta"
REDIRECTS = ["http://localhost/callback", "http://127.0.0.1/callback"]


@pytest.fixture
def c():
    with TestClient(door.app) as client:
        yield client


@pytest.fixture(autouse=True)
def _clean():
    door._pending.clear()
    door._codes.clear()
    yield


@pytest.fixture
def cimd_ok(monkeypatch):
    """A well-formed CIMD document, shaped exactly like the real one
    Claude Code publishes — PORTLESS loopback redirects included."""
    monkeypatch.setattr(door, "fetch_cimd", lambda cid, **k: {
        "client_id": cid, "client_name": "Test Client",
        "redirect_uris": list(REDIRECTS), "source": "cimd"})


def _pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(
        hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


def _authorize(c, **over):
    _v, ch = _pkce()
    q = {"response_type": "code", "client_id": CLIENT,
         "redirect_uri": "http://localhost:18789/callback",
         "code_challenge": ch, "code_challenge_method": "S256",
         "state": "client-state"}
    q.update(over)
    return c.get("/authorize", params=q, follow_redirects=False)


# --- discovery ----------------------------------------------------------------

def test_as_metadata_advertises_cimd_and_refuses_dcr(c):
    """The one flag that makes CIMD happen, and the endpoint that must
    never appear: a registration_endpoint would re-open DCR, which the
    owner refused permanently."""
    md = c.get("/.well-known/oauth-authorization-server").json()
    assert md["client_id_metadata_document_supported"] is True
    assert "registration_endpoint" not in md
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert md["grant_types_supported"] == ["authorization_code"]
    assert md["issuer"] == door.DOOR_ORIGIN


def test_protected_resource_metadata_both_paths(c):
    for p in ("/.well-known/oauth-protected-resource",
              "/.well-known/oauth-protected-resource/mcp"):
        b = c.get(p).json()
        assert b["resource"] == door.RESOURCE
        assert b["authorization_servers"] == [door.DOOR_ORIGIN]


def test_unauthenticated_mcp_call_points_at_the_metadata(c):
    r = c.post("/mcp", json={"method": "initialize"})
    assert r.status_code == 401
    assert "oauth-protected-resource" in r.headers["www-authenticate"]


# --- client identity ----------------------------------------------------------

def test_cimd_fetch_refuses_private_addresses(monkeypatch):
    """The SSRF guard. A client_id is attacker-chosen, and this host
    runs a loopback-bound admin API — resolving inward must be refused
    BEFORE the connection, not after."""
    monkeypatch.setattr(cimd.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ClientError, match="non-public"):
        cimd.fetch_cimd("https://evil.test/meta")


def test_cimd_fetch_refuses_plain_http():
    with pytest.raises(ClientError, match="https"):
        cimd.fetch_cimd("http://client.example/meta")


def test_cimd_document_must_claim_its_own_url(monkeypatch, httpx_stub):
    """Otherwise one hosted document could impersonate any client."""
    httpx_stub(200, {"client_id": "https://other.example/meta",
                     "redirect_uris": REDIRECTS})
    with pytest.raises(ClientError, match="does not match"):
        cimd.fetch_cimd("https://client.example/meta")


def test_cimd_fetch_does_not_follow_redirects(monkeypatch, httpx_stub):
    """A 302 is refused rather than followed: the target is chosen by
    the party the address check exists to contain."""
    httpx_stub(302, {})
    with pytest.raises(ClientError, match="HTTP 302"):
        cimd.fetch_cimd("https://client.example/meta")


# --- RFC 8252 loopback matching ----------------------------------------------

def test_loopback_port_variance_allowed_but_only_loopback():
    """claude-code #37747 in a unit test: the published URI is portless
    while the request carries a real port. RFC 8252 §7.3 says allow it —
    and NOT for anything else."""
    assert redirect_uri_allowed("http://localhost:54491/callback", REDIRECTS)
    assert redirect_uri_allowed("http://127.0.0.1:3118/callback", REDIRECTS)
    assert not redirect_uri_allowed("http://localhost:1/evil", REDIRECTS)
    assert not redirect_uri_allowed("https://evil.example/callback", REDIRECTS)
    assert not redirect_uri_allowed("http://evil.example:8080/callback",
                                    ["http://evil.example/callback"])
    assert not redirect_uri_allowed("http://localhost:8080/callback#x", REDIRECTS)


# --- authorize ----------------------------------------------------------------

def test_authorize_requires_pkce_s256(c, cimd_ok):
    r = _authorize(c, code_challenge_method="plain")
    assert r.status_code == 400 and "PKCE" in r.text


def test_authorize_rejects_unregistered_redirect_without_redirecting(c, cimd_ok):
    """The open-redirect guard: an unproven redirect_uri gets an HTML
    error, never a 302 carrying anything to it."""
    r = _authorize(c, redirect_uri="http://evil.example/steal")
    assert r.status_code == 400
    assert "location" not in {k.lower() for k in r.headers}


def test_authorize_redirects_to_the_idp_with_its_own_pkce(c, cimd_ok, monkeypatch):
    monkeypatch.setattr(door, "oidc_config", lambda: {
        "authorization_endpoint": "https://idp.test/authorize",
        "token_endpoint": "https://idp.test/token", "jwks_uri": "https://idp.test/jwks"})
    r = _authorize(c)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_id"] == [door.OIDC_CLIENT_ID]
    assert q["code_challenge_method"] == ["S256"]
    # the door's upstream verifier is its own, never the client's
    sid = q["state"][0]
    assert door._pending[sid]["verifier"] not in r.headers["location"]


def test_the_browser_is_sent_to_an_address_it_can_actually_reach(
        c, cimd_ok, monkeypatch):
    """The sign-in redirect is the ONE IdP url a browser follows, so it
    must carry the transport rewrite. Skipping it sent people to the
    IdP's advertised default port while the lab serves 8443 —
    `connection refused`, with the door itself perfectly healthy."""
    monkeypatch.setattr(door, "OIDC_HTTP_BASE", "https://idp.test:8443")
    monkeypatch.setattr(door, "oidc_config", lambda: {
        "authorization_endpoint": "https://idp.test/application/o/authorize/",
        "token_endpoint": "https://idp.test/token", "jwks_uri": "https://idp.test/jwks"})
    r = _authorize(c)
    assert urlparse(r.headers["location"]).netloc == "idp.test:8443"


# --- the full dance -----------------------------------------------------------

@pytest.fixture
def idp(monkeypatch):
    """Stub Authentik at the HTTP boundary: real JWT signing with a real
    RSA key, so id_token validation is exercised for real."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(door, "oidc_config", lambda: {
        "authorization_endpoint": "https://idp.test/authorize",
        "token_endpoint": "https://idp.test/token",
        "jwks_uri": "https://idp.test/jwks"})
    monkeypatch.setattr(door, "_idp_key", lambda kid: key.public_key())

    state = {"email": "alice@example.com", "sub": "idp-sub-alice"}

    class R:
        status_code = 200

        def json(self):
            import time
            tok = jwt.encode({"iss": door.OIDC_ISSUER, "aud": door.OIDC_CLIENT_ID,
                              "sub": state["sub"], "email": state["email"],
                              "name": "Alice", "iat": int(time.time()),
                              "exp": int(time.time()) + 300},
                             key, algorithm="RS256")
            return {"id_token": tok}

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return R()
        def get(self, *a, **k): return R()

    monkeypatch.setattr(door, "_http", lambda: C())
    return state


def _dance(c, cimd_ok_unused=None):
    """authorize → callback → token, returning the access token."""
    verifier, challenge = _pkce()
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": CLIENT,
        "redirect_uri": "http://localhost:18789/callback",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "client-state", "resource": door.RESOURCE},
        follow_redirects=False)
    assert r.status_code == 302, r.text
    sid = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    r = c.get("/callback", params={"code": "idp-code", "state": sid},
              follow_redirects=False)
    assert r.status_code == 302, r.text
    back = parse_qs(urlparse(r.headers["location"]).query)
    assert back["state"] == ["client-state"]
    code = back["code"][0]

    r = c.post("/token", data={"grant_type": "authorization_code", "code": code,
                               "redirect_uri": "http://localhost:18789/callback",
                               "client_id": CLIENT, "code_verifier": verifier})
    assert r.status_code == 200, r.text
    return code, verifier, r.json()["access_token"]


def test_full_sign_in_mints_a_resource_bound_token_and_a_principal(
        c, cimd_ok, idp):
    _code, _v, access = _dance(c)
    claims = jwt.decode(access, door.signing_key().public_key(),
                        algorithms=["RS256"], audience=door.RESOURCE,
                        issuer=door.DOOR_ORIGIN)
    assert claims["email"] == "alice@example.com"
    assert claims["aud"] == door.RESOURCE  # RFC 8707: cannot be replayed elsewhere
    with SessionLocal() as s:
        p = s.get(Principal, claims["sub"])
        assert p.email == "alice@example.com" and p.idp_sub == "idp-sub-alice"

    # The token opens the resource: a real MCP handshake answers.
    # (Policy-driven behaviour is test_door_mcp's subject; `initialize`
    # is deliberately the one method that needs no active store.)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-11-25"}},
               headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "sentinel-airlock"


def test_authorization_code_is_single_use(c, cimd_ok, idp):
    code, verifier, _access = _dance(c)
    r = c.post("/token", data={"grant_type": "authorization_code", "code": code,
                               "redirect_uri": "http://localhost:18789/callback",
                               "client_id": CLIENT, "code_verifier": verifier})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_token_refuses_a_wrong_pkce_verifier(c, cimd_ok, idp):
    verifier, challenge = _pkce()
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": CLIENT,
        "redirect_uri": "http://localhost:18789/callback",
        "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False)
    sid = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get("/callback", params={"code": "idp-code", "state": sid},
              follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = c.post("/token", data={"grant_type": "authorization_code", "code": code,
                               "redirect_uri": "http://localhost:18789/callback",
                               "client_id": CLIENT,
                               "code_verifier": "not-the-verifier"})
    assert r.status_code == 400
    assert "PKCE" in r.json()["error_description"]


def test_idp_sub_mismatch_refuses_sign_in(c, cimd_ok, idp):
    """TOFU pinning (ADR-005 D2) enforced at the door: same address,
    different IdP subject — a re-issued mailbox — is refused."""
    _dance(c)
    idp["sub"] = "idp-sub-someone-else"
    verifier, challenge = _pkce()
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": CLIENT,
        "redirect_uri": "http://localhost:18789/callback",
        "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False)
    sid = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get("/callback", params={"code": "idp-code", "state": sid},
              follow_redirects=False)
    assert r.status_code == 400 and "cannot sign in" in r.text


def test_disabled_principal_token_dies_on_the_next_call(c, cimd_ok, idp):
    """Why the door needs no refresh-token revocation store: authority
    is re-checked per call, so offboarding lands immediately."""
    idp["email"] = "bob@example.com"
    idp["sub"] = "idp-sub-bob"
    _code, _v, access = _dance(c)
    assert c.post("/mcp", json={}, headers={
        "Authorization": f"Bearer {access}"}).status_code == 200
    from app.models import utcnow
    with SessionLocal() as s:
        p = s.scalars(
            __import__("sqlalchemy").select(Principal).where(
                Principal.email == "bob@example.com")).first()
        p.disabled_at = utcnow()
        s.commit()
    assert c.post("/mcp", json={}, headers={
        "Authorization": f"Bearer {access}"}).status_code == 401


def test_a_token_for_another_resource_is_refused(c, cimd_ok, idp):
    """Audience binding proven negatively — a token minted for a
    different door must not open this one."""
    import time
    other = jwt.encode({"iss": door.DOOR_ORIGIN, "sub": "x", "aud": "https://elsewhere/mcp",
                        "iat": int(time.time()), "exp": int(time.time()) + 60},
                       door.signing_key(), algorithm="RS256")
    r = c.post("/mcp", json={}, headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 401


@pytest.fixture
def httpx_stub(monkeypatch):
    """Stub httpx at the client boundary for CIMD fetch tests."""
    def install(status, payload):
        class R:
            status_code = status
            content = json.dumps(payload).encode()

            def json(self):
                return payload

        class C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a, **k): return R()

        monkeypatch.setattr(cimd, "_assert_public_address", lambda h: None)
        monkeypatch.setattr(cimd.httpx, "Client", lambda **k: C())
    return install
