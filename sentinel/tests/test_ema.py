"""7.8.3 (ADR-008 D5): the EMA / ID-JAG receiver.

The suite's own ID-JAG signer (a real RSA key, real typ header) drives
the door's token endpoint through every refusal in the validation
chain, then the happy path end to end: a pinned principal's assertion
redeems for a door person-token that person_from_bearer accepts.

What is proven, in the order an attacker meets it:
  - grant refused entirely while EMA is disabled (and the metadata
    does not advertise it)
  - public clients refused (confidential-only, the draft's SHOULD)
  - typ != oauth-id-jag+jwt refused (token-confusion defense)
  - wrong aud / wrong iss / expired / over-long lifetime refused
  - jti replay refused on the second redemption
  - client_id claim != authenticated client refused
  - NO JIT: an assertion for a subject with no interactive pin
    refuses and creates nothing; email in the assertion never joins
  - disabled principal refused
  - happy path mints a token identical in shape to the interactive
    flow's, and no refresh_token rides the response

Run: python -m pytest tests/test_ema.py -q
"""
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

_TMP = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(_TMP, "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")
os.environ.setdefault("SENTINEL_DOOR_KEY", os.path.join(_TMP, "ema-door-key.pem"))
os.environ.setdefault("SENTINEL_DOOR_ORIGIN", "https://mcp.ema-test.local")
os.environ.setdefault("SENTINEL_POLICY_RELOAD_SECONDS", "0")
os.environ.setdefault("SENTINEL_EMA_ENABLED", "1")

import jwt  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import door, ema  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Principal  # noqa: E402
from app.service import get_or_create_principal, set_principal_disabled  # noqa: E402


def _migrate():
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location",
                        str(Path(__file__).resolve().parents[1] / "migrations"))
    command.upgrade(cfg, "head")


_migrate()

IDP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CLIENT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CLIENT_ID = "https://ema-client.example/meta"


def _client_jwk() -> dict:
    from jwt.algorithms import RSAAlgorithm
    jwk = json.loads(RSAAlgorithm.to_jwk(CLIENT_KEY.public_key()))
    jwk["kid"] = "ema-client-key"
    return jwk


@pytest.fixture
def c(monkeypatch):
    # Patch the door's OWN global, not the env: config imports once
    # per pytest process, and whichever test file sorts first freezes
    # it — the env setdefault above only wins when this file runs
    # standalone (the ADR-007 order-independence lesson, again).
    monkeypatch.setattr(door, "EMA_ENABLED", True)
    monkeypatch.setattr(door, "_idp_key", lambda kid: IDP_KEY.public_key())
    monkeypatch.setattr(
        door, "fetch_cimd",
        lambda cid, **k: {"client_id": cid, "client_name": "EMA client",
                          "redirect_uris": ["http://localhost/callback"],
                          "jwks": {"keys": [_client_jwk()]}})
    ema._seen_jti.clear()
    with TestClient(door.app) as client:
        yield client


def _sub() -> str:
    return f"ema-sub-{uuid.uuid4().hex[:8]}"


def _pin(email=None, sub=None) -> tuple[str, str]:
    """Interactive-sign-in stand-in: establish the (issuer, sub) pin
    the EMA path is allowed to ride."""
    email = email or f"ema-{uuid.uuid4().hex[:8]}@example.com"
    sub = sub or _sub()
    with SessionLocal() as s:
        get_or_create_principal(s, email=email, idp_sub=sub,
                                idp_iss=door.OIDC_ISSUER)
    return email, sub


def _id_jag(sub, *, typ=ema.IDJAG_TYP, aud=None, iss=None, client_id=CLIENT_ID,
            lifetime=120, iat=None, jti=None, extra=None):
    now = int(time.time())
    claims = {"iss": iss or door.OIDC_ISSUER, "sub": sub,
              "aud": aud or door.DOOR_ORIGIN, "client_id": client_id,
              "jti": jti or uuid.uuid4().hex,
              "iat": iat if iat is not None else now,
              "exp": (iat if iat is not None else now) + lifetime}
    claims.update(extra or {})
    return jwt.encode(claims, IDP_KEY, algorithm="RS256",
                      headers={"typ": typ})


def _client_assertion(client_id=CLIENT_ID):
    now = int(time.time())
    return jwt.encode(
        {"iss": client_id, "sub": client_id, "aud": door.DOOR_ORIGIN,
         "iat": now, "exp": now + 60, "jti": uuid.uuid4().hex},
        CLIENT_KEY, algorithm="RS256", headers={"kid": "ema-client-key"})


def _redeem(c, assertion, client_id=CLIENT_ID, with_client_auth=True):
    data = {"grant_type": ema.GRANT_TYPE, "assertion": assertion,
            "client_id": client_id}
    if with_client_auth:
        data["client_assertion_type"] = ema.CLIENT_ASSERTION_TYPE
        data["client_assertion"] = _client_assertion(client_id)
    return c.post("/token", data=data)


def test_metadata_advertises_the_profile_when_enabled(c):
    md = c.get("/.well-known/oauth-authorization-server").json()
    assert ema.GRANT_TYPE in md["grant_types_supported"]
    assert md["authorization_grant_profiles_supported"] == [ema.GRANT_PROFILE]
    assert "private_key_jwt" in md["token_endpoint_auth_methods_supported"]
    assert "registration_endpoint" not in md   # DCR stays dead, EMA or not


def test_disabled_ema_refuses_and_hides(c, monkeypatch):
    monkeypatch.setattr(door, "EMA_ENABLED", False)
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub))
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"
    md = c.get("/.well-known/oauth-authorization-server").json()
    assert ema.GRANT_TYPE not in md["grant_types_supported"]
    assert "authorization_grant_profiles_supported" not in md


def test_happy_path_mints_the_same_token_the_browser_flow_would(c):
    email, sub = _pin()
    r = _redeem(c, _id_jag(sub))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "refresh_token" not in body     # spec: none on this grant
    assert body["token_type"] == "Bearer"
    claims = jwt.decode(body["access_token"],
                        door.signing_key().public_key(),
                        algorithms=["RS256"], audience=door.RESOURCE,
                        issuer=door.DOOR_ORIGIN)
    assert claims["email"] == email
    assert claims["client_id"] == CLIENT_ID
    # and the ledger recorded the entrance
    from sqlalchemy import select as _sel
    from app.models import AuditEvent, AuditEventType
    with SessionLocal() as s:
        row = s.scalars(_sel(AuditEvent)
                        .where(AuditEvent.principal == email)
                        .order_by(AuditEvent.id.desc())).first()
        assert row.event_type == AuditEventType.AUTH_SUCCESS
        assert row.details["method"] == "ema-id-jag"


def test_public_client_refused_by_default(c):
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub), with_client_auth=False)
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"


def test_wrong_typ_refused(c):
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub, typ="JWT"))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_wrong_audience_refused(c):
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub, aud="https://some-other-as.example"))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_multi_audience_refused(c):
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub, aud=[door.DOOR_ORIGIN, "https://other"]))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_wrong_issuer_refused(c):
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub, iss="https://evil-idp.example"))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_expired_refused(c):
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub, iat=int(time.time()) - 400, lifetime=120))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_overlong_lifetime_refused(c):
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub, lifetime=3600))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_jti_replay_refused(c):
    _, sub = _pin()
    jag = _id_jag(sub)
    assert _redeem(c, jag).status_code == 200
    r = _redeem(c, jag)                      # same assertion, second time
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_client_mismatch_refused(c):
    """The assertion names who may redeem it — a different
    (authenticated!) client presenting it is refused."""
    _, sub = _pin()
    jag = _id_jag(sub, client_id="https://someone-else.example/meta")
    r = _redeem(c, jag)                      # we authenticate as CLIENT_ID
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_no_jit_unknown_subject_refused_and_nothing_created(c):
    ghost = _sub()
    before = _count_principals()
    r = _redeem(c, _id_jag(ghost, extra={"email": "ghost@example.com"}))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert _count_principals() == before     # the email claim joined NOTHING


def test_disabled_principal_refused(c):
    email, sub = _pin()
    with SessionLocal() as s:
        pid = s.scalars(__import__("sqlalchemy").select(Principal)
                        .where(Principal.email == email)).first().id
        set_principal_disabled(s, pid, disabled=True, actor="ema-test")
    r = _redeem(c, _id_jag(sub))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_garbage_client_assertion_is_audited_400_not_500(c):
    """Review-blocking find, pinned: attacker bytes in client_assertion
    escaped as an UNAUDITED 500. Now: 400 invalid_client + a paper
    trail."""
    _, sub = _pin()
    r = c.post("/token", data={
        "grant_type": ema.GRANT_TYPE, "assertion": _id_jag(sub),
        "client_id": CLIENT_ID,
        "client_assertion_type": ema.CLIENT_ASSERTION_TYPE,
        "client_assertion": "not-a-jwt-at-all"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"
    from sqlalchemy import select as _sel
    from app.models import AuditEvent, AuditEventType
    with SessionLocal() as s:
        row = s.scalars(_sel(AuditEvent).order_by(AuditEvent.id.desc())).first()
        assert row.event_type == AuditEventType.AUTH_FAILURE
        assert "not a JWT" in row.details["reason"]


def test_mixed_kty_jwks_skips_unusable_keys(c, monkeypatch):
    """An EC key FIRST in the client's CIMD jwks must be skipped, not
    crash the parse (review-probed as a 500)."""
    monkeypatch.setattr(
        door, "fetch_cimd",
        lambda cid, **k: {"client_id": cid,
                          "redirect_uris": ["http://localhost/callback"],
                          "jwks": {"keys": [{"kty": "EC", "crv": "P-256",
                                             "x": "AA", "y": "AA"},
                                            _client_jwk()]}})
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub))
    assert r.status_code == 200, r.text


def test_client_assertion_replay_refused(c):
    _, sub = _pin()
    ca = _client_assertion()
    data = {"grant_type": ema.GRANT_TYPE, "assertion": _id_jag(sub),
            "client_id": CLIENT_ID,
            "client_assertion_type": ema.CLIENT_ASSERTION_TYPE,
            "client_assertion": ca}
    assert c.post("/token", data=data).status_code == 200
    data["assertion"] = _id_jag(sub)          # fresh ID-JAG, SAME client auth
    r = c.post("/token", data=data)
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"


def test_overlong_client_assertion_refused(c):
    _, sub = _pin()
    now = int(time.time())
    ca = jwt.encode(
        {"iss": CLIENT_ID, "sub": CLIENT_ID, "aud": door.DOOR_ORIGIN,
         "iat": now, "exp": now + 86400 * 365, "jti": uuid.uuid4().hex},
        CLIENT_KEY, algorithm="RS256", headers={"kid": "ema-client-key"})
    r = c.post("/token", data={
        "grant_type": ema.GRANT_TYPE, "assertion": _id_jag(sub),
        "client_id": CLIENT_ID,
        "client_assertion_type": ema.CLIENT_ASSERTION_TYPE,
        "client_assertion": ca})
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"


def test_clock_skew_tolerated(c):
    """An IdP clock a few seconds AHEAD must not refuse fresh
    assertions (review-probed: PyJWT 2.13 rejects future iat with zero
    leeway — a whole-deployment EMA outage shaped like flakiness)."""
    _, sub = _pin()
    r = _redeem(c, _id_jag(sub, iat=int(time.time()) + 3))
    assert r.status_code == 200, r.text


def test_invalid_target_leaves_no_success_story(c):
    """A refused grant must not write AUTH_SUCCESS or bump last_seen
    (review-probed: the first draft committed success, THEN refused)."""
    email, sub = _pin()
    from sqlalchemy import select as _sel
    with SessionLocal() as s:
        before_seen = s.scalars(_sel(Principal).where(
            Principal.email == email)).first().last_seen_at
    r = _redeem(c, _id_jag(sub, extra={"resource": "https://other.example/mcp"}))
    assert r.status_code == 400 and r.json()["error"] == "invalid_target"
    from app.models import AuditEvent, AuditEventType
    with SessionLocal() as s:
        assert s.scalars(_sel(Principal).where(
            Principal.email == email)).first().last_seen_at == before_seen
        rows = s.scalars(_sel(AuditEvent)
                         .where(AuditEvent.principal == email)).all()
        assert not any(r2.event_type == AuditEventType.AUTH_SUCCESS
                       and r2.details.get("method") == "ema-id-jag"
                       for r2 in rows)


def test_hard_rule_ema_token_cannot_enter_the_elevation_doors(c, monkeypatch):
    """ADR-008 D5's hard rule as a TEST, not an aud-string accident: an
    EMA-minted access token, presented as the door_session cookie AND
    as a bearer, must bounce off /elevate to the interactive IdP leg."""
    monkeypatch.setattr(door, "oidc_config", lambda: {
        "authorization_endpoint": "https://idp.test/authorize",
        "token_endpoint": "https://idp.test/token",
        "jwks_uri": "https://idp.test/jwks"})
    _, sub = _pin()
    tok = _redeem(c, _id_jag(sub)).json()["access_token"]
    ticket = door._elevation_ticket(
        principal_id="whoever", email="whoever@example.com",
        outcome="confirm", profile="github:write", windows=[30],
        tool="github.create_pr")
    c.cookies.set("door_session", tok)
    r1 = c.get(f"/elevate/{ticket}", follow_redirects=False)
    c.cookies.delete("door_session")
    r2 = c.get(f"/elevate/{ticket}", follow_redirects=False,
               headers={"Authorization": f"Bearer {tok}"})
    assert r1.status_code == 302   # not the elevation page
    assert r2.status_code == 302


def test_real_jwks_cache_path_matches_kid(c, monkeypatch):
    """The suite's one un-stubbed run through door._idp_key: a real JWK
    (with kid) seeded into the discovery cache, the EMA happy path
    through actual kid-matching, and a wrong-kid refusal — so a JWKS-
    shape regression can no longer pass green (review-caught coverage
    gap: every other test replaces _idp_key wholesale)."""
    from jwt.algorithms import RSAAlgorithm
    monkeypatch.undo()                      # drop the fixture's stubs...
    monkeypatch.setattr(door, "EMA_ENABLED", True)  # ...keep EMA on
    monkeypatch.setattr(
        door, "fetch_cimd",
        lambda cid, **k: {"client_id": cid,
                          "redirect_uris": ["http://localhost/callback"],
                          "jwks": {"keys": [_client_jwk()]}})
    jwk = json.loads(RSAAlgorithm.to_jwk(IDP_KEY.public_key()))
    jwk["kid"] = "idp-rotation-key-7"
    door._oidc_cache["jwks"] = {"keys": [jwk]}
    try:
        _, sub = _pin()
        good = jwt.encode(
            {"iss": door.OIDC_ISSUER, "sub": sub, "aud": door.DOOR_ORIGIN,
             "client_id": CLIENT_ID, "jti": uuid.uuid4().hex,
             "iat": int(time.time()), "exp": int(time.time()) + 120},
            IDP_KEY, algorithm="RS256",
            headers={"typ": ema.IDJAG_TYP, "kid": "idp-rotation-key-7"})
        assert _redeem(c, good).status_code == 200
        # wrong kid: the one-shot cache refresh fires, finds nothing,
        # and the grant refuses — never a guess at a key
        bad = jwt.encode(
            {"iss": door.OIDC_ISSUER, "sub": sub, "aud": door.DOOR_ORIGIN,
             "client_id": CLIENT_ID, "jti": uuid.uuid4().hex,
             "iat": int(time.time()), "exp": int(time.time()) + 120},
            IDP_KEY, algorithm="RS256",
            headers={"typ": ema.IDJAG_TYP, "kid": "no-such-kid"})
        monkeypatch.setattr(door, "oidc_config",
                            lambda: {"jwks_uri": "https://idp.test/jwks"})
        class _NoNet:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a, **k):
                raise RuntimeError("no network in tests")
        monkeypatch.setattr(door, "_http", lambda: _NoNet())
        r = _redeem(c, bad)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    finally:
        door._oidc_cache.clear()


def _count_principals() -> int:
    from sqlalchemy import func, select as _sel
    with SessionLocal() as s:
        return s.execute(_sel(func.count()).select_from(Principal)).scalar_one()
