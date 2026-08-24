"""Sentinel Door — the PERSON-facing listener (7.3.3).

The third of Sentinel's three surfaces, one per population:

    broker  (mTLS, cluster-facing)   — pods and the Envoy proxy
    admin   (loopback, passkey)      — the human operator, kill switch
    door    (TLS, people)            — employees' MCP clients

The door is an MCP resource server AND its own minimal authorization
server. It is its own AS because the product ships Authentik as the
customer's IdP (ADR-005 D9 amendment) and Authentik does not speak the
dialect MCP clients want — CIMD client identity, RFC 9728 discovery,
resource-bound tokens. So the split is: **Authentik answers WHO you
are** (passwords, passkeys, sessions), **the door turns that into a
short-lived token bound to this resource**, and **the policy store
alone answers WHAT you may do** (ADR-005 P1 — a person unknown to the
store gets a perfectly valid token and `forbid` on every call).

Deliberately absent, permanently: **dynamic client registration**
(owner, 2026-08-02). Unauthenticated self-registration is the branch
the MCP spec deprecated; clients present a CIMD document or a
statically allowlisted id.

Also absent by design in 7.3.3: refresh tokens. Revocation that
matters happens per call — every request re-reads the principal ledger
and the policy store, so disabling a person takes effect on their next
call regardless of what token they hold. A refresh store would add
revocable state for an authority the token does not carry.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__
from . import ladder
from . import policy
from .cimd import ClientError, fetch_cimd, redirect_uri_allowed
from .config import (
    DOOR_KEY_PATH,
    DOOR_ORIGIN,
    DOOR_STATIC_CLIENTS,
    DOOR_TOKEN_TTL_MINUTES,
    EMA_ENABLED,
    MCP_UPSTREAMS,
    upstream_token,
    OIDC_CA_BUNDLE,
    OIDC_CLIENT_AUTH,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_EMAIL_CLAIM,
    OIDC_HTTP_BASE,
    OIDC_ISSUER,
    POLICY_RELOAD_SECONDS,
)
from .db import SessionLocal
from sqlalchemy import select

from . import ema
from .models import AuditEventType, Principal, utcnow
from .upstream_auth import UpstreamAuthError
from .service import (
    _norm_issuer,
    mint_forwarding_token,
    audit,
    create_request,
    get_or_create_principal,
    mint_profile_grant,
)

log = logging.getLogger("sentinel")

MCP_PATH = "/mcp"
RESOURCE = f"{DOOR_ORIGIN}{MCP_PATH}"
PENDING_TTL_SECONDS = 300   # a human has 5 minutes to finish signing in
CODE_TTL_SECONDS = 60       # an authorization code is a baton, not a token

# Short-lived, single-process, single-use state. Not database rows on
# purpose: a restart mid-sign-in should lose the half-finished dance
# (the client simply retries), and codes that outlive a restart are
# state an attacker can wait for.
_pending: dict[str, dict] = {}
_codes: dict[str, dict] = {}


def _sweep(store: dict, ttl: int) -> None:
    now = time.time()
    for k in [k for k, v in store.items() if now - v["t"] > ttl]:
        store.pop(k, None)


# --- the door's own signing key ----------------------------------------------

_key = None


def signing_key():
    """RSA key for the door's person-tokens, created on first start.
    0600 before any bytes are written — a key file that is briefly
    world-readable was briefly compromised."""
    global _key
    if _key is not None:
        return _key
    p = Path(DOOR_KEY_PATH)
    if p.exists():
        _key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        return _key
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    log.info("door signing key created at %s", p)
    _key = k
    return _key


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def door_jwk() -> dict:
    pub = signing_key().public_key().public_numbers()
    n = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")
    e = pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")
    jwk = {"kty": "RSA", "use": "sig", "alg": "RS256",
           "n": _b64u(n), "e": _b64u(e)}
    jwk["kid"] = _b64u(hashlib.sha256(
        json.dumps({"e": jwk["e"], "kty": "RSA", "n": jwk["n"]},
                   separators=(",", ":"), sort_keys=True).encode()).digest())[:16]
    return jwk


# --- the upstream IdP (Authentik) --------------------------------------------

_oidc_cache: dict = {}


def _transport_url(url: str) -> str:
    """Rewrite host:port for TRANSPORT only (see config.OIDC_HTTP_BASE).
    The logical issuer in `iss` is never rewritten — validation uses the
    issuer the IdP claims, transport uses the address that answers."""
    if not OIDC_HTTP_BASE:
        return url
    base, u = urlparse(OIDC_HTTP_BASE), urlparse(url)
    return urlunparse(u._replace(scheme=base.scheme, netloc=base.netloc))


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0, follow_redirects=False,
                        verify=OIDC_CA_BUNDLE or True)


def _idp_headers() -> dict:
    """Host pinning ONLY when the lab's split-horizon transport rewrite
    is active — unconditional pinning assumed the issuer host serves
    the token/JWKS endpoints too, which breaks IdPs whose endpoints
    live on a different host (custom-domain tenants; ADR-008 D2.3).
    Without a rewrite, the URL's own host is the right Host header."""
    if OIDC_HTTP_BASE:
        return {"Host": urlparse(OIDC_ISSUER).netloc}
    return {}


def oidc_config() -> dict:
    """Authentik's discovery document, cached. Fetched from the issuer
    (transport-rewritten); the `iss` inside it stays authoritative."""
    if "config" in _oidc_cache:
        return _oidc_cache["config"]
    url = _transport_url(OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration")
    with _http() as c:
        r = c.get(url, headers=_idp_headers())
        r.raise_for_status()
        cfg = r.json()
    _oidc_cache["config"] = cfg
    return cfg


def _idp_key(kid: str | None):
    """Authentik's signing key for id_token validation, cached with a
    one-shot refresh so a key rotation heals without a restart."""
    for attempt in (0, 1):
        jwks = _oidc_cache.get("jwks")
        if jwks is None or attempt:
            with _http() as c:
                r = c.get(_transport_url(oidc_config()["jwks_uri"]),
                          headers=_idp_headers())
                r.raise_for_status()
                jwks = _oidc_cache["jwks"] = r.json()
        for k in jwks.get("keys", []):
            if kid is None or k.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k))
    raise ClientError("no matching IdP signing key")


# --- app ----------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    signing_key()
    try:
        ap = policy.refresh(policy.POLICY_DIR)
        log.info("policy store active: %s", ap.version)
    except Exception as e:
        log.warning("policy store not active: %s", e)
    task = None
    if POLICY_RELOAD_SECONDS > 0:
        import asyncio
        task = asyncio.create_task(
            policy.watch_store(policy.POLICY_DIR, POLICY_RELOAD_SECONDS))
    yield
    if task:
        import asyncio
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    lifespan=lifespan,
    title="Sentinel Door (person-facing)",
    version=__version__,
    description="Sign in with the company identity; reach the tools your "
                "role should have. Authority comes from the policy store, "
                "never from this token.",
    docs_url=None, redoc_url=None,
)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    ap = policy.get_active()
    return {"status": "ok", "listener": "door", "version": __version__,
            "policy_version": ap.version if ap else None}


@app.get("/", tags=["meta"], response_class=HTMLResponse)
def index() -> HTMLResponse:
    """A door that answers 404 to a human looks broken (owner, first
    live visit — it was the first thing anyone tried). This is an
    address people are *given*, so it has to explain itself to whoever
    types it into a browser: machines get the metadata documents,
    people get a page."""
    active = policy.get_active()
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Airlock</title>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<body style='font-family:system-ui;max-width:40em;margin:4em auto;"
        "line-height:1.6'>"
        "<h1>Airlock</h1>"
        "<p>This address is for <strong>MCP clients</strong>, not browsers "
        "— there is nothing to click here. Point a client at it and sign "
        "in with your company account:</p>"
        f"<pre style='background:#f4f4f5;padding:1em;border-radius:6px;"
        f"overflow-x:auto'>claude mcp add --transport http airlock "
        f"{DOOR_ORIGIN}/mcp\nclaude mcp login airlock</pre>"
        "<p>You will then have the tools your role entitles you to. "
        "Signing in proves <em>who</em> you are; <strong>what</strong> you "
        "may do comes from the access policy — so if you sign in "
        "successfully and see no tools at all, you have not been added to "
        "it yet. Ask whoever runs this platform.</p>"
        f"<p style='color:#666;font-size:.9em'>Policy version: "
        f"{active.version if active else 'none active — nothing is reachable'}"
        f" · <a href='{DOOR_ORIGIN}/.well-known/oauth-protected-resource'>"
        "resource metadata</a></p>")


# --- discovery ----------------------------------------------------------------

def _prm() -> dict:
    """RFC 9728 protected-resource metadata: how a client learns which
    authorization server guards this resource. Here they are the same
    origin, which is allowed and keeps the deployment one component."""
    return {"resource": RESOURCE,
            "authorization_servers": [DOOR_ORIGIN],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
            "resource_documentation": f"{DOOR_ORIGIN}/"}


@app.get("/.well-known/oauth-protected-resource", tags=["discovery"])
@app.get("/.well-known/oauth-protected-resource/mcp", tags=["discovery"])
def protected_resource_metadata() -> dict:
    return _prm()


@app.get("/.well-known/oauth-authorization-server", tags=["discovery"])
@app.get("/.well-known/openid-configuration", tags=["discovery"])
def as_metadata() -> dict:
    return {
        "issuer": DOOR_ORIGIN,
        "authorization_endpoint": f"{DOOR_ORIGIN}/authorize",
        "token_endpoint": f"{DOOR_ORIGIN}/token",
        "jwks_uri": f"{DOOR_ORIGIN}/jwks",
        "response_types_supported": ["code"],
        # 7.8.3: the EMA/ID-JAG grant appears here ONLY when the
        # deployment enables it — advertising a grant nobody can
        # redeem invites probing (and the flag is how EMA clients
        # discover us at all).
        "grant_types_supported": (
            ["authorization_code", ema.GRANT_TYPE] if EMA_ENABLED
            else ["authorization_code"]),
        **({"authorization_grant_profiles_supported": [ema.GRANT_PROFILE]}
           if EMA_ENABLED else {}),
        # OAuth 2.1 posture, stated where clients can read it: no
        # implicit, no password, and PKCE is not optional.
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": (
            ["none", "private_key_jwt"] if EMA_ENABLED else ["none"]),
        "scopes_supported": ["mcp"],
        # The one flag that makes CIMD happen: clients only present a
        # URL client_id when the AS says it understands one.
        "client_id_metadata_document_supported": True,
        # No registration_endpoint — DCR is refused permanently.
        "resource_indicators_supported": True,
    }


@app.get("/jwks", tags=["discovery"])
def jwks() -> dict:
    return {"keys": [door_jwk()]}


# --- authorization ------------------------------------------------------------

def _err(message: str, status: int = 400) -> HTMLResponse:
    """Errors render HERE rather than redirecting to a client-supplied
    URL: until a redirect_uri is proven registered, sending anything to
    it makes the door an open redirector."""
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>Sign-in problem</title>"
        f"<body style='font-family:system-ui;max-width:40em;margin:4em auto'>"
        f"<h1>Sign-in problem</h1><p>{message}</p>"
        f"<p style='color:#666'>Nothing was granted. You can close this tab.</p>",
        status_code=status)


def resolve_client(client_id: str) -> dict:
    """Client identity: a CIMD document, or a statically allowlisted id.
    Never registration-on-demand."""
    if client_id in DOOR_STATIC_CLIENTS:
        return {"client_id": client_id, "client_name": client_id,
                "redirect_uris": None, "source": "static"}
    doc = fetch_cimd(client_id)
    doc["source"] = "cimd"
    return doc


@app.get("/authorize", tags=["oauth"])
def authorize(request: Request):
    _sweep(_pending, PENDING_TTL_SECONDS)
    q = request.query_params
    client_id, redirect_uri = q.get("client_id"), q.get("redirect_uri")
    if q.get("response_type") != "code":
        return _err("This client asked for an unsupported response type. "
                    "Only the authorization-code flow is supported.")
    if not client_id or not redirect_uri:
        return _err("The sign-in request was missing its client or return URL.")
    if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        return _err("This client did not use PKCE (S256), which is required.")

    try:
        client = resolve_client(client_id)
    except ClientError as e:
        return _err(f"This client could not be identified: {e}")
    if client["redirect_uris"] is not None and not redirect_uri_allowed(
            redirect_uri, client["redirect_uris"]):
        return _err("This client's return URL is not one it published.")
    if client["redirect_uris"] is None and urlparse(redirect_uri).hostname not in (
            "127.0.0.1", "::1", "localhost"):
        return _err("A statically registered client may only return to loopback.")

    sid = secrets.token_urlsafe(24)
    _pending[sid] = {
        "t": time.time(), "kind": "oauth",
        "client_id": client_id, "redirect_uri": redirect_uri,
        "client_state": q.get("state"), "challenge": q.get("code_challenge"),
        "resource": q.get("resource") or RESOURCE,
        "verifier": secrets.token_urlsafe(48),
        "client_name": client.get("client_name") or client_id,
    }
    return RedirectResponse(_upstream_login(sid), status_code=302)


def _upstream_login(sid: str) -> str:
    """Hand the human to the IdP, with the door's OWN PKCE verifier —
    never the client's. The door proves possession to Authentik; the
    client proves possession to the door. Two independent legs.

    The authorize URL is transport-rewritten like every other endpoint,
    and this one is the reason the rewrite cannot be skipped: it is the
    only IdP URL a BROWSER is sent to. Authentik advertises its
    endpoints on the default port, the lab serves it on 8443, and
    handing the raw discovery value to a browser produced exactly one
    symptom — `connection refused` on port 443, with the door itself
    perfectly healthy (observed 2026-08-02, first human sign-in).

    If a deployment ever has an IdP whose public address differs from
    the door's route to it, that needs its own setting; here and in
    cloud the two are the same."""
    challenge = _b64u(hashlib.sha256(_pending[sid]["verifier"].encode()).digest())
    # nonce (ADR-008 D2.4, OIDC Core): PKCE binds the CODE leg; the
    # nonce binds the ID_TOKEN to this browser session. Minted here,
    # checked in the callback's claims.
    nonce = _pending[sid].setdefault("nonce", secrets.token_urlsafe(24))
    return _transport_url(oidc_config()["authorization_endpoint"]) + "?" + urlencode({
        "response_type": "code", "client_id": OIDC_CLIENT_ID,
        "redirect_uri": f"{DOOR_ORIGIN}/callback",
        "scope": "openid email profile", "state": sid, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


@app.get("/callback", tags=["oauth"])
def callback(request: Request):
    """Back from Authentik. Turn their proof of WHO into our code."""
    _sweep(_pending, PENDING_TTL_SECONDS)
    q = request.query_params
    sess = _pending.pop(q.get("state") or "", None)
    if sess is None:
        return _err("This sign-in expired or was already used. "
                    "Start it again from your MCP client.")
    if sess.get("kind") == "link":
        # link-kind entries share the _pending store but belong to the
        # /link/{server}/callback leg — refusing here keeps their
        # nonce-less shape from ever reaching this validator
        # (review-caught: a truthy nonce guard is how a future producer
        # silently opts out of nonce checking).
        return _err("Wrong callback for this flow.")
    if q.get("error"):
        return _err(f"The identity provider refused: {q.get('error')}")
    code = q.get("code")
    if not code:
        return _err("The identity provider returned no authorization code.")

    data = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": f"{DOOR_ORIGIN}/callback",
            "client_id": OIDC_CLIENT_ID, "code_verifier": sess["verifier"]}
    # Confidential-client auth (ADR-008 D2.5): HTTP Basic is the OAuth
    # spec default and what Okta/Ping expect; `post` keeps the old
    # form-body shape for IdPs that want it. Public clients (no
    # secret) send neither — PKCE is their proof.
    basic_auth = None
    if OIDC_CLIENT_SECRET:
        if OIDC_CLIENT_AUTH == "post":
            data["client_secret"] = OIDC_CLIENT_SECRET
        else:
            basic_auth = (OIDC_CLIENT_ID, OIDC_CLIENT_SECRET)
            # With Basic auth the client_id must NOT also ride the
            # body: Okta 401s the mix verbatim — "Cannot supply
            # multiple client credentials" (found live, first real
            # Okta sign-in, 2026-08-23). RFC 6749 §3.2.1 only wants
            # body client_id for unauthenticated clients anyway.
            data.pop("client_id", None)
    try:
        with _http() as c:
            r = c.post(_transport_url(oidc_config()["token_endpoint"]), data=data,
                       headers=_idp_headers(), auth=basic_auth)
            if r.status_code != 200:
                raise ClientError(f"token exchange failed ({r.status_code})")
            tokens = r.json()
            id_token = tokens.get("id_token")
            if not id_token:
                raise ClientError("identity provider returned no id_token")
            header = jwt.get_unverified_header(id_token)
            claims = jwt.decode(
                id_token, _idp_key(header.get("kid")), algorithms=["RS256"],
                audience=OIDC_CLIENT_ID, issuer=OIDC_ISSUER,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]})
            # nonce binds the id_token to the session this door started
            # (ADR-008 D2.4) — MANDATORY, no truthiness escape: every
            # legitimate session gets one in _upstream_login, so a
            # missing nonce is a refusal, not a skip. azp: when aud is
            # multi-valued it must be present AND name us (the ADR's
            # "required" wording, deliberately stricter than OIDC
            # Core's SHOULD).
            if claims.get("nonce") != sess.get("nonce"):
                raise ClientError("id_token nonce mismatch")
            if isinstance(claims.get("aud"), list) and \
                    claims.get("azp") != OIDC_CLIENT_ID:
                raise ClientError("id_token azp missing or mismatched")
            # Email claim mapping (ADR-008 D2.2): the configured claim
            # first; ONE userinfo call as fallback (some IdPs — Entra
            # without the optional claim — put identity there); then a
            # hard refusal, because a person without a stable email
            # cannot exist in the policy store.
            email = (claims.get(OIDC_EMAIL_CLAIM) or "").strip().lower()
            if not email and tokens.get("access_token"):
                ui_url = oidc_config().get("userinfo_endpoint")
                if ui_url:
                    ui = c.get(_transport_url(ui_url), headers={
                        **_idp_headers(),
                        "Authorization": f"Bearer {tokens['access_token']}"})
                    if ui.status_code == 200:
                        ui_claims = ui.json()
                        # OIDC Core 5.3.2: userinfo values are only
                        # usable if its sub matches the id_token's —
                        # the cheap defense against token substitution.
                        # Honest bound, stated: on this path the email
                        # is TLS-attested, not signature-attested; the
                        # (iss, sub) pin stays signature-grade either
                        # way.
                        if ui_claims.get("sub") == claims.get("sub"):
                            email = (ui_claims.get(OIDC_EMAIL_CLAIM)
                                     or "").strip().lower()
    except (ClientError, jwt.PyJWTError, httpx.HTTPError, ValueError) as e:
        log.warning("door sign-in failed: %s", e)
        with SessionLocal() as s:
            audit(s, AuditEventType.AUTH_FAILURE,
                  details={"surface": "door", "reason": str(e)[:200]})
            s.commit()
        return _err("Sign-in could not be completed.")

    if not email:
        return _err("Your identity provider did not release an email address "
                    f"(looked for the `{OIDC_EMAIL_CLAIM}` claim; "
                    "SENTINEL_OIDC_EMAIL_CLAIM changes which).")
    # Vendor-stable recovery id (never a join key): Entra's oid+tid
    # survives an app re-registration that its pairwise sub does not.
    stable = None
    if claims.get("oid") and claims.get("tid"):
        stable = f"oid:{claims['oid']}@{claims['tid']}"
    try:
        with SessionLocal() as s:
            p = get_or_create_principal(
                s, email=email, idp_sub=claims.get("sub"),
                idp_iss=claims.get("iss"), idp_stable_id=stable,
                display_name=claims.get("name"))
            principal_id, principal_email = p.id, p.email
            audit(s, AuditEventType.AUTH_SUCCESS, principal=p.email,
                  details={"surface": "door",
                           "client": sess.get("client_name", "browser")})
            s.commit()
    except ValueError as e:  # principal-disabled, idp-sub-mismatch (audited)
        return _err("Your account cannot sign in here. "
                    f"({e}) Contact your platform administrator.")

    if sess.get("kind") == "browser":
        # The door's own pages (the elevation doors, 7.3.5). No
        # authorization code is minted: a browser session is not an
        # API credential and cannot be exchanged for one.
        r = RedirectResponse(sess["return_to"], status_code=302)
        # path="/" and not "/elevate": the narrower scope meant /link
        # could NEVER receive the cookie, so account-linking was an
        # infinite redirect loop (found by ADR-008's review; fixed in
        # 7.8.1). Widening costs nothing security-wise — the cookie is
        # a door-signed JWT that only the browser-page handlers read,
        # and httponly/secure/samesite still apply everywhere.
        r.set_cookie("door_session", _session_cookie(principal_id, principal_email),
                     max_age=SESSION_TTL_SECONDS, httponly=True, secure=True,
                     samesite="lax", path="/")
        return r

    ac = secrets.token_urlsafe(32)
    _codes[ac] = {"t": time.time(), "client_id": sess["client_id"],
                  "redirect_uri": sess["redirect_uri"],
                  "challenge": sess["challenge"], "resource": sess["resource"],
                  "principal_id": principal_id, "email": principal_email}
    params = {"code": ac}
    if sess["client_state"] is not None:
        params["state"] = sess["client_state"]
    sep = "&" if urlparse(sess["redirect_uri"]).query else "?"
    return RedirectResponse(f"{sess['redirect_uri']}{sep}{urlencode(params)}",
                            status_code=302)


def _mint_person_token(*, principal_id: str, email: str, resource: str,
                       client_id: str) -> dict:
    """The door's person-token, one mint for both entrances (the
    interactive code flow and 7.8.3's EMA grant — a person who arrived
    via ID-JAG must hold exactly the token an interactive sign-in
    yields, no more).

    JWT time claims are epoch seconds and MUST come from an
    epoch-native clock. models.utcnow() is naive-UTC (the DB
    convention), and .timestamp() on a naive datetime silently
    applies the host's LOCAL offset — which stamped every token six
    hours into the future on this box and made it "not yet valid" to
    any correct validator. Two time conventions, one of them
    invisible: use time.time() here, never the DB helper."""
    now = int(time.time())
    claims = {
        "iss": DOOR_ORIGIN, "sub": principal_id, "email": email,
        # Audience-bound to the resource the client named (RFC 8707,
        # which Claude Code sends and Authentik would have ignored): a
        # token minted for this door cannot be replayed at another.
        "aud": resource, "client_id": client_id,
        "iat": now, "exp": now + DOOR_TOKEN_TTL_MINUTES * 60,
        "jti": secrets.token_urlsafe(12),
    }
    access = jwt.encode(claims, signing_key(), algorithm="RS256",
                        headers={"kid": door_jwk()["kid"]})
    return {"access_token": access, "token_type": "Bearer",
            "expires_in": DOOR_TOKEN_TTL_MINUTES * 60, "scope": "mcp"}


@app.post("/token", tags=["oauth"])
def token(grant_type: str = Form(...), code: str = Form(None),
          redirect_uri: str = Form(None), client_id: str = Form(None),
          code_verifier: str = Form(None), assertion: str = Form(None),
          client_assertion_type: str = Form(None),
          client_assertion: str = Form(None)):
    _sweep(_codes, CODE_TTL_SECONDS)
    if EMA_ENABLED and grant_type == ema.GRANT_TYPE:
        return _token_id_jag(assertion, client_id,
                             client_assertion_type, client_assertion)
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, 400)
    rec = _codes.pop(code or "", None)  # single use: popped before validation
    if rec is None:
        return JSONResponse({"error": "invalid_grant"}, 400)
    if rec["client_id"] != client_id or rec["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant"}, 400)
    if not code_verifier or _b64u(hashlib.sha256(
            code_verifier.encode()).digest()) != rec["challenge"]:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "PKCE verification failed"}, 400)
    return _mint_person_token(
        principal_id=rec["principal_id"], email=rec["email"],
        resource=rec["resource"], client_id=rec["client_id"])


def _token_id_jag(assertion, client_id, client_assertion_type,
                  client_assertion):
    """7.8.3 (ADR-008 D5): redeem an enterprise IdP's ID-JAG for the
    door's person-token. Validation lives in app/ema.py; what lives
    HERE is the ledger join and its refusals — because who a subject
    IS was decided by an interactive sign-in's (issuer, sub) pin, and
    this grant only ever rides that decision, never makes one (no JIT:
    an assertion for a person the ledger has never seen refuses, and
    the assertion's email claim is advisory, never a join key)."""
    def _fail(reason: str, code: str = "invalid_grant", email: str | None = None):
        with SessionLocal() as s:
            audit(s, AuditEventType.AUTH_FAILURE, principal=email,
                  details={"surface": "door", "method": "ema-id-jag",
                           "reason": reason[:200]})
            s.commit()
        log.warning("ema grant refused: %s", reason)
        return JSONResponse({"error": code}, 400)

    if not assertion:
        return _fail("missing assertion")
    try:
        caller = ema.authenticate_client(
            client_id, client_assertion_type, client_assertion,
            fetch_cimd=fetch_cimd)
        claims = ema.validate_id_jag(
            assertion, authenticated_client=caller, idp_key=_idp_key)
    except ema.GrantError as e:
        return _fail(str(e), e.code)
    except ClientError as e:
        # cimd.fetch_cimd's own refusals (SSRF guard, bad document)
        # must be an invalid_client, not an unhandled 500.
        return _fail(f"client metadata: {e}", "invalid_client")
    # The resource check runs BEFORE the ledger join — it needs only
    # the claims, and a refused grant must never leave an AUTH_SUCCESS
    # row or a last_seen bump behind it (review-probed: the first
    # draft committed success, then refused).
    resource = claims.get("resource") or RESOURCE
    if resource != RESOURCE:
        return _fail("assertion resource does not name this deployment's "
                     "MCP resource", "invalid_target")
    iss, sub = claims["iss"], claims["sub"]
    with SessionLocal() as s:
        # ALL rows sharing this sub, filtered by normalized issuer —
        # the composite unique deliberately allows one sub under two
        # issuers, so a bare `.first()` by sub could return another
        # issuer's row and refuse a legitimate person.
        rows = s.scalars(select(Principal).where(
            Principal.idp_sub == sub)).all()
        matches = [r for r in rows
                   if _norm_issuer(r.idp_iss) == _norm_issuer(iss)]
        if len(matches) > 1:
            # Two pins normalize onto one (issuer, sub) — a state the
            # raw composite unique permits only via a trailing-slash
            # habit change at the IdP. Impossible in practice, so it
            # refuses LOUDLY rather than picking quietly.
            return _fail("ambiguous (issuer, sub) pin — multiple ledger "
                         "rows match this assertion")
        p = matches[0] if matches else None
        if p is None:
            return _fail("unknown subject — no interactively-established "
                         "(issuer, sub) pin for this assertion (no JIT)")
        if p.disabled_at is not None:
            return _fail("principal disabled", email=p.email)
        p.last_seen_at = utcnow()
        audit(s, AuditEventType.AUTH_SUCCESS, principal=p.email,
              details={"surface": "door", "method": "ema-id-jag",
                       "client": caller, "jti": claims["jti"],
                       "iss": iss})
        s.commit()
        principal_id, email = p.id, p.email
    return _mint_person_token(principal_id=principal_id, email=email,
                              resource=resource, client_id=caller)


# --- the resource ------------------------------------------------------------

class TokenError(Exception):
    pass


def person_from_bearer(request: Request) -> dict:
    """Validate a door token and return the claims. Signature, issuer,
    audience and expiry are checked by the library; the LEDGER check is
    ours and is why a disabled person's token dies on its next call
    rather than at expiry."""
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        raise TokenError("missing token")
    try:
        claims = jwt.decode(h[7:].strip(), signing_key().public_key(),
                            algorithms=["RS256"], audience=RESOURCE,
                            issuer=DOOR_ORIGIN,
                            options={"require": ["exp", "iat", "sub", "aud"]})
    except jwt.PyJWTError as e:
        raise TokenError(str(e))
    with SessionLocal() as s:
        p = s.get(Principal, claims["sub"])
        if p is None or p.disabled_at is not None:
            raise TokenError("principal disabled or unknown")
    return claims


def _unauthorized(detail: str) -> JSONResponse:
    """401 carrying the RFC 9728 pointer — this header is how an MCP
    client discovers where to sign in."""
    return JSONResponse(
        {"error": "unauthorized", "error_description": detail}, 401,
        headers={"WWW-Authenticate":
                 f'Bearer resource_metadata='
                 f'"{DOOR_ORIGIN}/.well-known/oauth-protected-resource/mcp"'})


# --- MCP protocol (7.3.4) -----------------------------------------------------
#
# The door is ONE address fronting every MCP server (CLAUDE.md's
# promise: "point an MCP client at one address"). Tools are namespaced
# `<server>.<leaf>` — the same string the ladder decides on and the
# audit log records, so what a person saw, what they called, and what
# was allowed are all the same identifier.
#
# Request/response only: no SSE. That is a deliberate 7.3.4 boundary,
# and it settles ADR-005's audit gap 1 (an elevation's expiry cannot
# close an already-open stream) the simplest way — there are no open
# streams to outlive a policy change, and every call re-decides against
# the live policy version. When server-initiated notifications need
# SSE, the stream cap must arrive in the same change.

_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def _rpc_error(rid, code: int, message: str, data: dict | None = None) -> dict:
    err = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _tool_entry(name: str, outcome: str) -> dict:
    borrowed = outcome != "permit"
    note = {"confirm": " Requires a timed elevation you can start yourself.",
            "approve": " Requires approval from another person."}.get(outcome, "")
    return {
        "name": name,
        "description": f"{name} via Airlock.{note}",
        "inputSchema": {"type": "object", "additionalProperties": True},
        "_meta": {"airlock/outcome": outcome, "airlock/elevation": borrowed},
    }


def _call_upstream(server: str, leaf: str, arguments: dict, *,
                   principal_id: str, principal_email: str,
                   tool: str, flow_id: str) -> dict:
    """Forward an ALLOWED call to the server's real upstream — through
    the sentinel-proxy, the same enforcement point in-cluster callers
    use. Not around it: "nothing reaches an MCP server without a
    capability check" has to stay literally true, or the door becomes a
    second unguarded entrance to everything the proxy protects.

    So the door mints itself a one-call, 30-second, scope-locked token
    (service.mint_forwarding_token) and presents it the way the proxy
    expects. That costs the human nothing — the approval question was
    answered upstairs by the ladder — while the proxy independently
    re-checks the kill switch and the scope it derives FROM THE REQUEST
    ITSELF, which is the property that makes the second check worth
    making rather than ceremonial."""
    # The connection's own address wins; the env map stays as the
    # deployment-level fallback so existing installs are unaffected.
    from .config import MCP_PROXY_BASE, MCP_UPSTREAM_TOKENS_FILE
    from .upstream_auth import upstream_url, is_registered
    # In order: the connection's explicit address, then a deployment
    # override, then THIS platform's own proxy path — so registering a
    # server that runs here needs no address at all.
    url = upstream_url(server, MCP_UPSTREAM_TOKENS_FILE) or MCP_UPSTREAMS.get(server)
    if not url and is_registered(server, MCP_UPSTREAM_TOKENS_FILE):
        url = f"{MCP_PROXY_BASE}/{server}/mcp"
    if not url:
        return {"content": [{"type": "text", "text":
                             f"Allowed by policy, but no upstream is "
                             f"configured for '{server}' on this "
                             f"deployment."}], "isError": True}
    # Identity travels as plain VALUES, never as an ORM object: the
    # session that loaded it is already closed by the time we get here,
    # and a detached instance raises the moment anything touches a
    # lazily-loaded attribute. (It did, on the first live call.)
    with SessionLocal() as s:
        try:
            token = mint_forwarding_token(
                s, flow_id=flow_id, tool=tool,
                principal=s.get(Principal, principal_id))
        except ValueError as e:  # kill switch
            return {"content": [{"type": "text", "text": str(e)}],
                    "isError": True}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": leaf, "arguments": arguments}}
    headers = {
        "X-Sentinel-Token": token, "X-Flow-Id": flow_id,
        "X-Airlock-Principal": principal_email,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # LAYER 2, and in http mode it is not belt-and-braces: GitHub's
        # server reads `X-MCP-Toolsets` / `X-MCP-Readonly` /
        # `X-MCP-Exclude-Tools` from the REQUEST, so a caller could
        # widen its own toolset by sending them. We set them explicitly
        # on every forward so a client-supplied value can never survive;
        # the server's CLI flags remain the ceiling this cannot exceed.
        "X-MCP-Toolsets": "", "X-MCP-Readonly": "", "X-MCP-Exclude-Tools": "",
    }
    # The upstream credential is injected HERE, by the component that
    # just authorized the call — the workload holds none. A compromised
    # MCP server pod therefore has nothing to steal.
    try:
        upstream_secret = upstream_token(server, caller=principal_email)
    except UpstreamAuthError as e:
        # Refuse loudly, and — when the fix is the person linking their
        # own account — hand back the link that does it, the same way a
        # policy refusal carries its elevation link.
        log.warning("upstream credential unavailable for %s: %s", server, e)
        text = f"This platform's credential for '{server}' is not usable ({e})."
        if "link your account" in str(e) or "linked account" in str(e):
            text = (f"{e}. Open this link to connect your own {server} "
                    f"account: {DOOR_ORIGIN}/link/{server}")
        return {"content": [{"type": "text", "text": text}], "isError": True}
    if upstream_secret:
        headers["Authorization"] = f"Bearer {upstream_secret}"
    try:
        with _http() as c:
            r = c.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        return {"content": [{"type": "text", "text":
                             f"Upstream '{server}' unreachable: {e}"}],
                "isError": True}
    # Record the EFFECT, not just the decision (7.6 item 1). Until now
    # a permitted call that the upstream rejected and one that created
    # a pull request looked identical in the audit log. Status, latency
    # and the upstream's own object id are enough to reconstruct what
    # happened; the PAYLOAD is deliberately not stored — PR bodies and
    # file contents are a privacy and retention liability, and the
    # upstream keeps its own content log.
    with SessionLocal() as s:
        audit(s, AuditEventType.USE, flow_id=flow_id, tool=tool,
              principal=principal_email,
              details={"source": "door-upstream", "server": server,
                       "http_status": r.status_code,
                       "ms": int(r.elapsed.total_seconds() * 1000)})
        s.commit()

    if r.status_code == 403:
        # The proxy refused what the ladder allowed. That is a real
        # disagreement between two enforcement layers, not a user error
        # — say so plainly instead of dressing it as a tool failure.
        return {"content": [{"type": "text", "text":
                             f"The enforcement proxy refused this call "
                             f"({r.text[:200]}). Policy allowed it, so "
                             f"this is a platform fault worth "
                             f"reporting."}], "isError": True}
    if r.status_code != 200:
        return {"content": [{"type": "text", "text":
                             f"Upstream '{server}' returned "
                             f"HTTP {r.status_code}."}], "isError": True}
    payload = _first_json_rpc(r)
    return payload.get("result") or {
        "content": [{"type": "text",
                     "text": json.dumps(payload.get("error", {}))}],
        "isError": True}


def _first_json_rpc(r: httpx.Response) -> dict:
    """MCP servers may answer a POST as JSON or as a one-message SSE
    stream (`text/event-stream`), and which one is the SERVER's choice,
    not ours — so handle both rather than assuming."""
    if "text/event-stream" in r.headers.get("content-type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        return {"error": {"message": "unreadable event stream"}}
    try:
        return r.json()
    except ValueError:
        return {"error": {"message": r.text[:200]}}


def _handle_rpc(msg: dict, claims: dict, flow_id: str) -> dict | None:
    """One JSON-RPC message → one response (None for notifications)."""
    rid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        want = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": want if want in _PROTOCOL_VERSIONS
                               else _PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "sentinel-airlock", "version": __version__,
                           "title": "Airlock"},
            "instructions": "Tools are named <server>.<tool>. You see only "
                            "what your role entitles. Tools marked as "
                            "needing elevation can be unlocked for a "
                            "time-boxed window.",
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    ap = policy.get_active()
    if ap is None:
        return _rpc_error(rid, -32001, "Airlock has no active policy; "
                                       "nothing is reachable.")

    if method == "tools/list":
        visible = ladder.visible_tools(ap, claims["email"])
        with SessionLocal() as s:
            # ONE audit row for the listing (see visible_tools: the
            # per-tool evaluations are deliberately silent).
            audit(s, AuditEventType.USE, flow_id=flow_id, tool="rpc.tools_list",
                  principal=claims["email"], policy_version=ap.version,
                  details={"source": "door", "visible": len(visible)})
            s.commit()
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "tools": [_tool_entry(n, o) for n, o in sorted(visible.items())]}}

    if method == "tools/call":
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        server, _, leaf = name.partition(".")
        with SessionLocal() as s:
            p = s.get(Principal, claims["sub"])
            if p is None:
                return _rpc_error(rid, -32002, "Unknown principal.")
            principal_id, principal_email = p.id, p.email
            # `arguments` is passed as the JSON-RPC params.arguments
            # RECORD itself — the store's resource map (`from:
            # params.arguments.repo`) walks the keys inside it. Wrapping
            # it one level deeper makes every mapped tool derive
            # `unmapped-resource` and deny closed: safe, but silently
            # unusable.
            result = ladder.decide(s, principal=p, tool=name,
                                   arguments=arguments)
        if not result.allowed:
            # The refusal is the product: it says what borrowing would
            # take AND hands over the one-time link that does it, so a
            # client can offer "unlock this?" instead of a dead end.
            data = {"outcome": result.outcome, "reason": result.reason,
                    "resource": result.resource, "policy_version": ap.version}
            message = "Not permitted."
            if result.hint:
                ticket = _elevation_ticket(
                    principal_id=claims["sub"], email=claims["email"],
                    outcome=result.outcome, profile=result.hint["profile"],
                    windows=result.hint["windows"], tool=name)
                url = f"{DOOR_ORIGIN}/elevate/{ticket}"
                data["elevation"] = {**result.hint, "url": url}
                # The URL goes in the MESSAGE, not only in `data`: MCP
                # clients surface the message string and may drop the
                # structured payload entirely, which turned "open the
                # link in elevation.url" into an instruction pointing at
                # something invisible (found live, 2026-08-02 — the
                # calling agent correctly refused to go hunting for it).
                # A refusal that names a remedy has to carry it.
                message = (
                    f"This tool needs a timed elevation you can start "
                    f"yourself. Open this link and confirm a window "
                    f"({'/'.join(str(w) for w in result.hint['windows'])} "
                    f"minutes): {url}"
                    if result.reason == "elevation-available" else
                    f"This tool needs approval from a different person. "
                    f"Open this link to request it: {url}")
            return _rpc_error(rid, -32003, message, data)
        return {"jsonrpc": "2.0", "id": rid,
                "result": _call_upstream(server, leaf, arguments,
                                         principal_id=principal_id,
                                         principal_email=principal_email,
                                         tool=name, flow_id=flow_id)}

    return _rpc_error(rid, -32601, f"Method not supported: {method}")


@app.post(MCP_PATH, tags=["mcp"])
async def mcp(request: Request):
    try:
        claims = person_from_bearer(request)
    except TokenError as e:
        return _unauthorized(str(e))
    try:
        msg = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), 400)

    # Person-flow ids are minted HERE, never accepted from the client
    # (7.2.1 note): unique by construction, so two people's sessions
    # can never collide and every audit row attributes correctly.
    flow_id = f"person-{secrets.token_urlsafe(9)}"

    if isinstance(msg, list):  # JSON-RPC batch
        out = [r for r in (_handle_rpc(m, claims, flow_id) for m in msg)
               if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    if not isinstance(msg, dict):
        return JSONResponse(_rpc_error(None, -32600, "Invalid Request"), 400)
    resp = _handle_rpc(msg, claims, flow_id)
    return Response(status_code=202) if resp is None else JSONResponse(resp)


# --- the elevation doors (7.3.5) ---------------------------------------------
#
# A refusal that says "you could borrow this" is only half a product;
# these are the doors that let someone act on it.
#
#   confirm  — the caller elevates THEMSELVES: sudo-shaped, time-boxed,
#              recorded, no second person.
#   approve  — a DIFFERENT human decides, on the passkey console. Same
#              card and same button as 5.5's flow; `granted_via` is
#              what distinguishes the two, and only `approve` (or an
#              operator's `admin`) satisfies the ladder's approve rung.
#
# **Why a browser page and not an MCP tool.** An `airlock.elevate`
# tool would be callable by the MODEL, and a model that can elevate
# itself is precisely the self-granting hole this architecture exists
# to close (ADR-005: anything may draft, only a human activates). The
# elevation therefore happens where the model cannot reach: a page
# behind the company IdP, requiring a click by the signed-in person.
# The MCP-native alternative — the spec's `elicitation` capability,
# which Claude Code advertises — routes the prompt to the human
# through the client and is the better UX, but it needs a
# server→client stream, and 7.3.4 deliberately has no SSE. Named as
# the upgrade, not pretended.

SESSION_TTL_SECONDS = 900
TICKET_TTL_SECONDS = 900
_tickets: dict[str, dict] = {}


def _session_cookie(principal_id: str, email: str) -> str:
    now = int(time.time())
    return jwt.encode({"iss": DOOR_ORIGIN, "aud": "door-session",
                       "sub": principal_id, "email": email, "iat": now,
                       "exp": now + SESSION_TTL_SECONDS},
                      signing_key(), algorithm="RS256")


def _session(request: Request) -> dict | None:
    raw = request.cookies.get("door_session")
    if not raw:
        return None
    try:
        sess = jwt.decode(raw, signing_key().public_key(), algorithms=["RS256"],
                          audience="door-session", issuer=DOOR_ORIGIN)
    except jwt.PyJWTError:
        return None
    # The offboarding switch must invalidate BROWSER sessions too, not
    # only API bearers (review-caught: a just-disabled principal could
    # otherwise elevate or link for up to the cookie's 15 minutes) —
    # same one-line ledger re-read person_from_bearer already does.
    with SessionLocal() as s:
        p = s.get(Principal, sess.get("sub", ""))
        if p is None or p.disabled_at is not None:
            return None
    return sess


def _elevation_ticket(*, principal_id: str, email: str, outcome: str,
                      profile: str, windows: list[int], tool: str) -> str:
    _sweep(_tickets, TICKET_TTL_SECONDS)
    tid = secrets.token_urlsafe(24)
    _tickets[tid] = {"t": time.time(), "principal_id": principal_id,
                     "email": email, "outcome": outcome, "profile": profile,
                     "windows": windows, "tool": tool}
    return tid


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{title}</title>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<body style='font-family:system-ui;max-width:34em;margin:4em auto;"
        "line-height:1.5'>" + body, status_code=status)


@app.get("/elevate/{ticket}", tags=["elevation"])
def elevate_page(ticket: str, request: Request):
    _sweep(_tickets, TICKET_TTL_SECONDS)
    t = _tickets.get(ticket)
    if t is None:
        return _page("Expired", "<h1>This elevation link has expired</h1>"
                     "<p>Ask again from your client and use the new link.</p>", 404)
    sess = _session(request)
    if sess is None:
        # Sign in first, then come back here. Same federation as the
        # API path; the session cookie it sets is door-wide (path=/,
        # 7.8.1 — /link needs it too) and cannot be exchanged for a
        # token.
        sid = secrets.token_urlsafe(24)
        _pending[sid] = {"t": time.time(), "kind": "browser",
                         "verifier": secrets.token_urlsafe(48),
                         "return_to": f"{DOOR_ORIGIN}/elevate/{ticket}"}
        return RedirectResponse(_upstream_login(sid), status_code=302)
    if sess["sub"] != t["principal_id"]:
        # The link names a person; only that person may act on it.
        return _page("Not yours", "<h1>This request belongs to someone else</h1>"
                     "<p>You are signed in as "
                     f"{sess['email']}.</p>", 403)

    csrf = _b64u(hashlib.sha256(
        (ticket + str(signing_key().private_numbers().d)).encode()).digest())[:32]
    if t["outcome"] == "confirm":
        buttons = "".join(
            f"<button name=minutes value={m} style='font-size:1.1em;"
            f"padding:.6em 1.2em;margin-right:.5em'>{m} minutes</button>"
            for m in t["windows"])
        body = (f"<h1>Unlock <code>{t['profile']}</code>?</h1>"
                f"<p>You asked to run <code>{t['tool']}</code>. That needs "
                "elevated access, which you can grant yourself for a limited "
                "time. Everything you do in the window is recorded, and it "
                "closes by itself.</p>"
                f"<form method=post><input type=hidden name=csrf value={csrf}>"
                f"{buttons}</form>")
    else:
        body = (f"<h1>Ask for approval on <code>{t['profile']}</code>?</h1>"
                f"<p><code>{t['tool']}</code> is high-risk here: a different "
                "person has to approve it. This puts a request on the "
                "Sentinel console for whoever holds a passkey.</p>"
                f"<form method=post><input type=hidden name=csrf value={csrf}>"
                f"<input type=hidden name=minutes value={t['windows'][0]}>"
                "<button style='font-size:1.1em;padding:.6em 1.2em'>"
                "Request approval</button></form>")
    return _page("Airlock", body)


@app.post("/elevate/{ticket}", tags=["elevation"])
def elevate_submit(ticket: str, request: Request, csrf: str = Form(...),
                   minutes: int = Form(...)):
    t = _tickets.get(ticket)
    sess = _session(request)
    if t is None or sess is None:
        return _page("Expired", "<h1>This elevation link has expired</h1>", 404)
    if sess["sub"] != t["principal_id"]:
        return _page("Not yours", "<h1>This request belongs to someone else</h1>",
                     403)
    expected = _b64u(hashlib.sha256(
        (ticket + str(signing_key().private_numbers().d)).encode()).digest())[:32]
    if not secrets.compare_digest(csrf, expected):
        return _page("Refused", "<h1>That form did not come from here</h1>", 400)
    if minutes not in t["windows"]:
        return _page("Refused", "<h1>That is not one of the offered windows</h1>",
                     400)

    _tickets.pop(ticket, None)  # single use, whichever door it opened
    server, _, level = t["profile"].partition(":")
    ap = policy.get_active()
    tools = policy.profile_tools(ap.servers, server, level) if ap else []
    if not tools:
        return _page("Unavailable", "<h1>That profile covers no tools</h1>"
                     "<p>The policy may have changed. Try your call again.</p>",
                     409)

    with SessionLocal() as s:
        p = s.get(Principal, t["principal_id"])
        if t["outcome"] == "confirm":
            try:
                mint_profile_grant(
                    s, principal=p, profile=t["profile"], tools=tools,
                    window_minutes=minutes, granted_via="confirm",
                    granted_by=p.email,
                    flow_id=None)
            except ValueError as e:  # kill switch engaged
                return _page("Unavailable", f"<h1>Not right now</h1><p>{e}</p>", 409)
            return _page("Unlocked",
                         f"<h1>Unlocked for {minutes} minutes</h1>"
                         f"<p><code>{t['profile']}</code> is available until the "
                         "window closes. Go back to your client and try the "
                         "call again — you do not need to sign in again.</p>")
        flow_id = f"elev-{secrets.token_urlsafe(9)}"
        req, _created = create_request(
            s, flow_id=flow_id, tool=f"profile:{t['profile']}",
            reason=f"{p.email} asked to run {t['tool']}",
            agent=f"airlock-door:{p.email}",
            claim_nonce=secrets.token_urlsafe(16))
        req.principal_id, req.profile, req.window_minutes = p.id, t["profile"], minutes
        s.commit()
    return _page("Waiting", "<h1>Sent for approval</h1>"
                 "<p>Someone with a Sentinel passkey has to approve this. "
                 "You will be able to make the call once they do — no need "
                 "to sign in again.</p>")


# --- linking your own account at an upstream (7.7) ---------------------------
#
# A person authorises the upstream themselves, once, and Sentinel holds
# THEIR token. Same shape as the elevation doors and for the same
# reason: it happens in a browser behind the company sign-in, where the
# model cannot follow. A model that could link accounts could link
# somebody else's.

@app.get("/link/{server}", tags=["elevation"])
def link_start(server: str, request: Request):
    sess = _session(request)
    if sess is None:
        sid = secrets.token_urlsafe(24)
        _pending[sid] = {"t": time.time(), "kind": "browser",
                         "verifier": secrets.token_urlsafe(48),
                         "return_to": f"{DOOR_ORIGIN}/link/{server}"}
        return RedirectResponse(_upstream_login(sid), status_code=302)
    from .config import MCP_UPSTREAM_TOKENS_FILE
    from . import upstream_auth
    state = secrets.token_urlsafe(24)
    _pending[state] = {"t": time.time(), "kind": "link", "server": server,
                       "caller": sess["email"], "verifier": ""}
    try:
        url = upstream_auth.link_start_url(
            server, MCP_UPSTREAM_TOKENS_FILE,
            f"{DOOR_ORIGIN}/link/{server}/callback", state)
    except upstream_auth.UpstreamAuthError as e:
        return _page("Not available", f"<h1>Cannot link {server}</h1><p>{e}</p>",
                     409)
    return RedirectResponse(url, status_code=302)


@app.get("/link/{server}/callback", tags=["elevation"])
def link_callback(server: str, request: Request):
    from .config import MCP_UPSTREAM_TOKENS_FILE
    from . import upstream_auth
    sess = _pending.pop(request.query_params.get("state") or "", None)
    if sess is None or sess.get("kind") != "link":
        return _page("Expired", "<h1>That link expired</h1>"
                     "<p>Start again from your client.</p>", 400)
    code = request.query_params.get("code")
    if not code:
        return _page("Refused", f"<h1>{server} did not authorise</h1>", 400)
    try:
        upstream_auth.link_complete(server, MCP_UPSTREAM_TOKENS_FILE, code,
                                    sess["caller"],
                                    f"{DOOR_ORIGIN}/link/{server}/callback")
    except upstream_auth.UpstreamAuthError as e:
        return _page("Refused", f"<h1>Could not link {server}</h1><p>{e}</p>", 400)
    with SessionLocal() as s:
        audit(s, AuditEventType.CREDENTIAL_ADDED, principal=sess["caller"],
              details={"kind": "upstream-account-linked", "server": server})
        s.commit()
    return _page("Linked",
                 f"<h1>{server} is linked to your account</h1>"
                 f"<p>Calls you make now act as <strong>you</strong> at "
                 f"{server} — its own audit log will name you, and its own "
                 f"permissions apply. Go back to your client.</p>")


@app.get(MCP_PATH, tags=["mcp"])
async def mcp_stream(request: Request):
    """MCP's optional server→client stream. Refused deliberately (see
    the section comment): without an open stream, no session can
    outlive the policy that authorized it."""
    try:
        person_from_bearer(request)
    except TokenError as e:
        return _unauthorized(str(e))
    return JSONResponse(_rpc_error(None, -32004,
                                   "This door does not open server-initiated "
                                   "streams; send requests as POST."), 405)
