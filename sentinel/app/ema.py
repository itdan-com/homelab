"""7.8.3 (ADR-008 D5): the EMA / ID-JAG RECEIVER — enterprise SSO into
Airlock with zero consent screens.

The wire (MCP Enterprise-Managed Authorization, stable 2026-06-17,
implementing draft-ietf-oauth-identity-assertion-authz-grant-04): the
person signed into their MCP client at the ENTERPRISE IdP once; the
client silently exchanged that session for an ID-JAG — a short-lived
JWT the IdP minted naming this door as its audience — and presents it
at our token endpoint under the jwt-bearer grant. We validate it,
map the person, and mint the door's own person-token exactly as the
interactive flow would have. No browser, no redirect, no consent.

The validation discipline is Keycloak's experimental receiver, read at
source and adopted deliberately (each rule is a refusal an attacker
would otherwise exploit):

  typ MUST be oauth-id-jag+jwt      — token-confusion defense: an
                                      id_token or access token must
                                      never redeem as a grant
  iss MUST be the deployment issuer — one IdP per deployment
                                      (ADR-008 D1); its JWKS signs
  aud MUST be exactly OUR issuer id — an assertion for another AS
                                      dies here; multi-audience is
                                      refused outright (Keycloak's
                                      stance, stricter than the draft)
  exp/iat REQUIRED, lifetime capped — a long-lived assertion is a
                                      bearer credential pretending
                                      not to be one (cap: 300s)
  jti single-use, FORCED            — replay cache; no config can
                                      disable it (Keycloak ignores
                                      its own reuse knob for this typ)
  client_id claim == the client     — the assertion names who may
    authenticating on this request    redeem it; anyone else is
                                      refused even with a valid JWT
  subject mapping: NO JIT, ever     — the (issuer, sub) pin from an
                                      INTERACTIVE sign-in is the only
                                      join; an ID-JAG for someone the
                                      ledger has never seen refuses.
                                      email in the assertion is
                                      advisory and never a join key.

What EMA never touches (ADR-008 D5's hard rule): the elevation
ceremonies. An EMA token signs a person IN; /elevate and /link still
require the interactive browser leg, and no machine-exchanged
assertion can mint a door_session or satisfy a confirm/approve rung.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import jwt

from . import cimd
from .config import (
    DOOR_ORIGIN,
    EMA_ALLOW_PUBLIC_CLIENTS,
    EMA_MAX_ASSERTION_SECONDS,
    OIDC_ISSUER,
)

log = logging.getLogger("sentinel")

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
GRANT_PROFILE = "urn:ietf:params:oauth:grant-profile:id-jag"
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
IDJAG_TYP = "oauth-id-jag+jwt"

# jti replay cache: {jti: seen_at}, guarded by a lock because /token
# is a sync endpoint on FastAPI's threadpool — two simultaneous
# redemptions of one assertion must not both pass a check-then-insert
# (review-caught TOCTOU). In-process is a NAMED bound, not an
# accident: a door restart empties the cache, so an already-redeemed
# assertion could replay for the remainder of its <=300s validity IF
# the attacker holds its bytes and lands a restart inside the window —
# accepted in ADR-008 D5's as-built note; the durable upgrade is a
# jti-primary-key table in Sentinel's DB.
_seen_jti: dict[str, float] = {}
_jti_lock = threading.Lock()


class GrantError(Exception):
    """Refusals surface as RFC 6749 invalid_grant/invalid_client with
    the reason logged and audited, never echoed raw to the caller
    beyond the OAuth error code."""

    def __init__(self, reason: str, code: str = "invalid_grant"):
        super().__init__(reason)
        self.code = code


def _sweep_jti(now: float) -> None:
    dead = [j for j, t in _seen_jti.items()
            if now - t > EMA_MAX_ASSERTION_SECONDS * 2]
    for j in dead:
        del _seen_jti[j]


def authenticate_client(client_id: str | None,
                        client_assertion_type: str | None,
                        client_assertion: str | None,
                        fetch_cimd=cimd.fetch_cimd) -> str:
    """Who is redeeming this assertion, proven not claimed.

    The draft SHOULD-limits this profile to confidential clients and
    Keycloak enforces it; we follow. A CIMD client is confidential by
    carrying keys in its metadata document and signing a
    private_key_jwt client assertion with them (RFC 7523 §2.2):
    iss == sub == client_id, aud names this AS, exp bounded, signature
    verifies against the document's jwks. The escape hatch for a
    future client that ships public-only
    (SENTINEL_EMA_ALLOW_PUBLIC_CLIENTS) accepts the bare client_id —
    a documented weakening, never the default."""
    if not client_id:
        raise GrantError("missing client_id", "invalid_client")
    if not client_assertion:
        if EMA_ALLOW_PUBLIC_CLIENTS:
            return client_id
        raise GrantError(
            "this grant requires a confidential client "
            "(private_key_jwt via the client's CIMD keys)",
            "invalid_client")
    if client_assertion_type != CLIENT_ASSERTION_TYPE:
        raise GrantError("unknown client_assertion_type", "invalid_client")
    doc = fetch_cimd(client_id)
    # Inline `jwks` only — a jwks_uri would need its own SSRF-guarded
    # fetch pipeline; refusing is fail-closed and documented in
    # docs/idp-registration.md (known interop bound, not an accident).
    keys = (doc.get("jwks") or {}).get("keys") or []
    if not keys:
        raise GrantError("client metadata document carries no usable keys "
                         "(inline jwks required; jwks_uri unsupported)",
                         "invalid_client")
    # Every parse below handles ATTACKER-CONTROLLED bytes (the
    # assertion is anonymous input; the CIMD document is
    # attacker-hosted) — nothing may escape as a 500, because an
    # unaudited refusal on a public endpoint is an audit-blindness
    # primitive (review-caught, probe-confirmed).
    try:
        header = jwt.get_unverified_header(client_assertion)
    except jwt.PyJWTError as e:
        raise GrantError(f"client assertion is not a JWT: {e}",
                         "invalid_client")
    kid = header.get("kid")
    key = None
    for k in keys:
        if kid is not None and k.get("kid") != kid:
            continue
        try:
            key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k))
            break
        except Exception:
            continue   # unusable/non-RSA key: skip, a later key may fit
    if key is None:
        raise GrantError("no matching client key", "invalid_client")
    try:
        claims = jwt.decode(
            client_assertion, key, algorithms=["RS256"],
            audience=[DOOR_ORIGIN, f"{DOOR_ORIGIN}/token"], leeway=10,
            options={"require": ["exp", "iat", "jti", "iss", "sub", "aud"]})
    except jwt.PyJWTError as e:
        raise GrantError(f"client assertion invalid: {e}", "invalid_client")
    if claims.get("iss") != client_id or claims.get("sub") != client_id:
        raise GrantError("client assertion iss/sub must equal client_id",
                         "invalid_client")
    # Same discipline as the ID-JAG one function down (review-caught:
    # without these, a captured client assertion was a decade-valid
    # reusable credential): bounded lifetime, single-use jti.
    if claims["exp"] - claims["iat"] > EMA_MAX_ASSERTION_SECONDS:
        raise GrantError("client assertion lifetime exceeds cap",
                         "invalid_client")
    with _jti_lock:
        _sweep_jti(time.time())
        ca_key = f"ca:{claims['jti']}"
        if ca_key in _seen_jti:
            raise GrantError("client assertion replay", "invalid_client")
        _seen_jti[ca_key] = time.time()
    return client_id


def validate_id_jag(assertion: str, *, authenticated_client: str,
                    idp_key, now: float | None = None) -> dict:
    """draft-04 §4.4.1, in the order an attacker meets it. Returns the
    validated claims; raises GrantError on any refusal. `idp_key` is a
    callable(kid) -> public key against the deployment IdP's JWKS —
    the door's existing _idp_key, injected so tests stay hermetic."""
    now = time.time() if now is None else now
    try:
        header = jwt.get_unverified_header(assertion)
    except jwt.PyJWTError as e:
        raise GrantError(f"not a JWT: {e}")
    if header.get("typ") != IDJAG_TYP:
        raise GrantError(
            f"typ must be {IDJAG_TYP} (token-confusion defense)")
    try:
        key = idp_key(header.get("kid"))
    except Exception as e:
        raise GrantError(f"no IdP key: {e}")
    try:
        # leeway=10: PyJWT 2.13 refuses a FUTURE iat, so an IdP clock
        # 1-3s ahead of this host would refuse every fresh assertion —
        # a total EMA outage presenting as intermittent invalid_grant
        # (review-probed). Ten seconds tolerates clocks; the 300s
        # lifetime cap below is exp-iat and clock-independent.
        claims = jwt.decode(
            assertion, key, algorithms=["RS256"],
            audience=DOOR_ORIGIN, issuer=OIDC_ISSUER, leeway=10,
            options={"require": ["exp", "iat", "sub", "iss",
                                 "aud", "client_id", "jti"]})
    except jwt.PyJWTError as e:
        raise GrantError(f"assertion invalid: {e}")
    # multi-audience refused outright: an assertion honored by more
    # than one AS is an assertion whose blast radius nobody bounded.
    if isinstance(claims.get("aud"), list) and len(claims["aud"]) != 1:
        raise GrantError("multi-audience assertions are refused")
    if claims["exp"] - claims["iat"] > EMA_MAX_ASSERTION_SECONDS:
        raise GrantError(
            f"assertion lifetime exceeds {EMA_MAX_ASSERTION_SECONDS}s cap")
    if claims["client_id"] != authenticated_client:
        raise GrantError(
            "assertion was issued to a different client than the one "
            "redeeming it")
    with _jti_lock:
        _sweep_jti(now)
        jti = claims["jti"]
        if jti in _seen_jti:
            raise GrantError("assertion replay (jti already redeemed)")
        _seen_jti[jti] = now
    return claims
