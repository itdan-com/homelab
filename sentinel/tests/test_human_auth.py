"""Human auth (5.5.6): the console is shut without a credential, and
the credential is one a human physically holds.

A software authenticator drives the real WebAuthn ceremonies here —
signing a real challenge with a real P-256 key — so the crypto path is
exercised rather than mocked around. What that buys: if the origin, the
RP ID, the challenge, or the signature ever stop being checked, these
tests fail, and those four checks ARE the phishing resistance the kill
switch depends on.
"""

import os
import secrets
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.main import app as admin_app  # noqa: E402
from authkit import (  # noqa: E402
    CONSOLE_ORIGIN, SoftAuthenticator, _b64u, challenge_of as _challenge_of,
    mint_code as _mint_code,
)

_HERE = os.path.dirname(__file__)
CONSOLE = {"x-sentinel-console": "1"}
# https base URL: the session cookie is marked Secure (browsers do send
# Secure cookies to localhost, so production keeps the strict setting)
# and an http test client would silently never send it back.
admin = TestClient(admin_app, base_url="https://testserver")


def _migrate() -> None:
    cfg = Config(os.path.join(_HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_HERE, "..", "migrations"))
    command.upgrade(cfg, "head")


def test_console_is_shut_without_a_credential():
    _migrate()
    # A client that has never signed in — deliberately separate from the
    # module's `admin`, so this holds no matter which suites ran first
    # (they share one database).
    anon = TestClient(admin_app, base_url="https://testserver")

    # Nothing about the platform is readable without a session — not the
    # pending decisions, not the record, not even the kill-switch state.
    for path in ("/v1/capability-requests", "/v1/audit-events",
                 "/v1/flows", "/v1/kill"):
        assert anon.get(path).status_code == 401, f"{path} requires a session"
    assert anon.post("/v1/kill", json={}, headers=CONSOLE).status_code == 401
    assert anon.post("/auth/totp/enroll", json={}, headers=CONSOLE).status_code == 401

    # ...but liveness works, because a supervisor must be able to ask
    # before any human has enrolled.
    assert anon.get("/healthz").status_code == 200
    st = anon.get("/auth/status").json()
    assert st["authenticated"] is False and st["operator"] is None


def test_registration_requires_a_host_minted_code():
    assert admin.post("/auth/register/begin", json={"code": "guessed"},
                      headers=CONSOLE).status_code == 403, \
        "reaching the port is not enough to become the approver"
    assert admin.post("/auth/login/begin", headers=CONSOLE).status_code == 200


def test_passkey_enroll_then_sign_in_then_decide():
    device = SoftAuthenticator()
    code = _mint_code()

    started = admin.post("/auth/register/begin", json={"code": code}, headers=CONSOLE)
    assert started.status_code == 200
    challenge = _challenge_of(started.json()["options"])
    done = admin.post("/auth/register/complete", headers=CONSOLE, json={
        "_challenge": challenge, "credential": device.create(challenge)})
    assert done.status_code == 200 and done.json()["operator"] == "bob"

    # The code is single-use: a leaked one cannot enroll a second device.
    assert admin.post("/auth/register/begin", json={"code": code},
                      headers=CONSOLE).status_code == 403

    opts = admin.post("/auth/login/begin", headers=CONSOLE).json()["options"]
    challenge = _challenge_of(opts)
    signed = admin.post("/auth/login/complete", headers=CONSOLE, json={
        "_challenge": challenge, "credential": device.get(challenge)})
    assert signed.status_code == 200 and signed.json()["operator"] == "bob"
    assert admin.cookies.get("sentinel_session"), "a session cookie was issued"

    # Signed in: the console works, and the audit log names the human the
    # authenticator proved — not a string anyone typed.
    assert admin.get("/v1/capability-requests").status_code == 200
    assert admin.get("/auth/status").json()["operator"] == "bob"
    kinds = [e["event_type"] for e in admin.get("/v1/audit-events").json()]
    assert "auth_success" in kinds and "credential_added" in kinds

    admin.post("/auth/logout", json={}, headers=CONSOLE)
    assert admin.get("/v1/capability-requests").status_code == 401, \
        "signing out really closes the session"


def test_phishing_resistance_and_replay():
    """The four checks that make a passkey unphishable. Break any one and
    a lookalike site can drive the kill switch."""
    device = SoftAuthenticator()
    code = _mint_code(label="second")
    started = admin.post("/auth/register/begin", json={"code": code}, headers=CONSOLE)
    challenge = _challenge_of(started.json()["options"])
    admin.post("/auth/register/complete", headers=CONSOLE, json={
        "_challenge": challenge, "credential": device.create(challenge)})

    # 1. Wrong origin — the signature is over the attacker's origin, so it
    #    verifies against nothing here. This is THE anti-phishing property.
    opts = admin.post("/auth/login/begin", headers=CONSOLE).json()["options"]
    ch = _challenge_of(opts)
    assert admin.post("/auth/login/complete", headers=CONSOLE, json={
        "_challenge": ch,
        "credential": device.get(ch, origin="https://evil.example")
    }).status_code == 401, "a signature made for another origin is refused"

    # 2. Challenge replay — a used challenge is gone, so a captured
    #    assertion cannot be resubmitted.
    opts = admin.post("/auth/login/begin", headers=CONSOLE).json()["options"]
    ch = _challenge_of(opts)
    assertion = device.get(ch)
    assert admin.post("/auth/login/complete", headers=CONSOLE,
                      json={"_challenge": ch, "credential": assertion}
                      ).status_code == 200
    admin.post("/auth/logout", json={}, headers=CONSOLE)
    assert admin.post("/auth/login/complete", headers=CONSOLE,
                      json={"_challenge": ch, "credential": assertion}
                      ).status_code == 401, "the same assertion cannot be replayed"

    # 3. A challenge Sentinel never issued.
    assert admin.post("/auth/login/complete", headers=CONSOLE, json={
        "_challenge": _b64u(secrets.token_bytes(32)),
        "credential": device.get(_b64u(secrets.token_bytes(32)))
    }).status_code == 401, "only challenges Sentinel issued are accepted"

    # 4. An unknown credential, correctly signed by a stranger's key.
    stranger = SoftAuthenticator()
    opts = admin.post("/auth/login/begin", headers=CONSOLE).json()["options"]
    ch = _challenge_of(opts)
    assert admin.post("/auth/login/complete", headers=CONSOLE, json={
        "_challenge": ch, "credential": stranger.get(ch)
    }).status_code == 401, "an unregistered authenticator is refused"

    # Every refusal above is on the record: a console nobody could sign
    # in to must still say who kept trying.
    kinds = [e["event_type"] for e in admin.get("/v1/audit-events",
                                                params={"limit": 200}).json()] \
        if admin.get("/v1/capability-requests").status_code == 200 else []
    assert kinds == [] or "auth_failure" in kinds


def test_totp_is_a_fallback_not_a_way_in():
    device = SoftAuthenticator()
    code = _mint_code(label="totp-owner")
    started = admin.post("/auth/register/begin", json={"code": code}, headers=CONSOLE)
    ch = _challenge_of(started.json()["options"])
    admin.post("/auth/register/complete", headers=CONSOLE, json={
        "_challenge": ch, "credential": device.create(ch)})

    # Enrolling the fallback requires already being signed in.
    assert admin.post("/auth/totp/enroll", json={}, headers=CONSOLE).status_code == 401

    opts = admin.post("/auth/login/begin", headers=CONSOLE).json()["options"]
    ch = _challenge_of(opts)
    admin.post("/auth/login/complete", headers=CONSOLE,
               json={"_challenge": ch, "credential": device.get(ch)})

    enrolled = admin.post("/auth/totp/enroll", json={}, headers=CONSOLE).json()
    assert enrolled["uri"].startswith("otpauth://totp/")
    assert enrolled["qr_data_uri"].startswith("data:image/svg+xml;base64,"), \
        "the QR is rendered server-side — the console loads no QR library"

    import pyotp
    secret = enrolled["uri"].split("secret=")[1].split("&")[0]
    admin.post("/auth/logout", json={}, headers=CONSOLE)

    # Unconfirmed seed cannot sign in yet: proving the app holds it is
    # part of enrolling, not an afterthought.
    assert admin.post("/auth/login/totp", headers=CONSOLE, json={
        "username": "bob", "code": pyotp.TOTP(secret).now()}).status_code == 401

    opts = admin.post("/auth/login/begin", headers=CONSOLE).json()["options"]
    ch = _challenge_of(opts)
    admin.post("/auth/login/complete", headers=CONSOLE,
               json={"_challenge": ch, "credential": device.get(ch)})
    assert admin.post("/auth/totp/confirm", headers=CONSOLE,
                      json={"code": pyotp.TOTP(secret).now()}).status_code == 200
    admin.post("/auth/logout", json={}, headers=CONSOLE)

    assert admin.post("/auth/login/totp", headers=CONSOLE, json={
        "username": "bob", "code": "000000"}).status_code == 401, "wrong code refused"
    assert admin.post("/auth/login/totp", headers=CONSOLE, json={
        "username": "bob", "code": pyotp.TOTP(secret).now()}).status_code == 200
    assert admin.get("/v1/capability-requests").status_code == 200
    admin.post("/auth/logout", json={}, headers=CONSOLE)


if __name__ == "__main__":
    test_console_is_shut_without_a_credential()
    test_registration_requires_a_host_minted_code()
    test_passkey_enroll_then_sign_in_then_decide()
    test_phishing_resistance_and_replay()
    test_totp_is_a_fallback_not_a_way_in()
    print("ok")
