"""Test-side WebAuthn authenticator and sign-in helper.

Every suite that touches the admin console needs a session now, so the
software authenticator lives here rather than in one test file. It is a
real authenticator — real P-256 key, real signature over
authenticatorData || SHA256(clientDataJSON) — so the suites exercise the
actual crypto path instead of a mock of it.
"""

import base64
import json
import os
import secrets
import struct
from hashlib import sha256

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import CONSOLE_ORIGIN, CONSOLE_RP_ID

CONSOLE = {"x-sentinel-console": "1"}

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class SoftAuthenticator:
    """The minimum honest WebAuthn authenticator: a P-256 key, an
    RP-ID hash, a flag byte, a counter, and a signature over
    authenticatorData || SHA256(clientDataJSON)."""

    def __init__(self, rp_id: str = CONSOLE_RP_ID):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = secrets.token_bytes(32)
        self.rp_id_hash = sha256(rp_id.encode()).digest()
        self.sign_count = 0

    def _auth_data(self, *, attested: bool) -> bytes:
        # UP | UV | (AT when attesting) — user present and user verified,
        # which the server REQUIRES: presence alone would let a key sitting
        # in a laptop approve on its own.
        flags = 0x01 | 0x04 | (0x40 if attested else 0)
        data = self.rp_id_hash + bytes([flags]) + struct.pack(">I", self.sign_count)
        if attested:
            pub = self.key.public_key().public_numbers()
            cose = cbor2.dumps({
                1: 2, 3: -7, -1: 1,
                -2: pub.x.to_bytes(32, "big"),
                -3: pub.y.to_bytes(32, "big"),
            })
            data += (b"\x00" * 16 + struct.pack(">H", len(self.cred_id))
                     + self.cred_id + cose)
        return data

    def _client_data(self, typ: str, challenge_b64u: str, origin: str) -> bytes:
        return json.dumps({"type": typ, "challenge": challenge_b64u,
                           "origin": origin, "crossOrigin": False}).encode()

    def create(self, challenge_b64u: str, origin: str = CONSOLE_ORIGIN) -> dict:
        client_data = self._client_data("webauthn.create", challenge_b64u, origin)
        att = cbor2.dumps({"fmt": "none", "attStmt": {},
                           "authData": self._auth_data(attested=True)})
        return {"id": _b64u(self.cred_id), "rawId": _b64u(self.cred_id),
                "type": "public-key",
                "response": {"clientDataJSON": _b64u(client_data),
                             "attestationObject": _b64u(att)}}

    def get(self, challenge_b64u: str, origin: str = CONSOLE_ORIGIN) -> dict:
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", challenge_b64u, origin)
        auth_data = self._auth_data(attested=False)
        signature = self.key.sign(auth_data + sha256(client_data).digest(),
                                  ec.ECDSA(hashes.SHA256()))
        return {"id": _b64u(self.cred_id), "rawId": _b64u(self.cred_id),
                "type": "public-key",
                "response": {"clientDataJSON": _b64u(client_data),
                             "authenticatorData": _b64u(auth_data),
                             "signature": _b64u(signature),
                             "userHandle": None}}




def challenge_of(options_json: str) -> str:
    return json.loads(options_json)["challenge"]


def mint_code(username="bob", label="test-key") -> str:
    from app.auth import mint_enrollment_code
    from app.db import SessionLocal
    with SessionLocal() as s:
        return mint_enrollment_code(s, username, label)


def sign_in(client, username="bob", label="test-key") -> SoftAuthenticator:
    """Enroll a passkey and open a console session on `client`.

    Suites call this right after migrating: since 5.5.6 the console has
    no anonymous surface, so "authenticate first" is simply what using
    it looks like.
    """
    device = SoftAuthenticator()
    code = mint_code(username, label)
    started = client.post("/auth/register/begin", json={"code": code}, headers=CONSOLE)
    ch = challenge_of(started.json()["options"])
    client.post("/auth/register/complete", headers=CONSOLE,
                json={"_challenge": ch, "credential": device.create(ch)})
    opts = client.post("/auth/login/begin", headers=CONSOLE).json()["options"]
    ch = challenge_of(opts)
    client.post("/auth/login/complete", headers=CONSOLE,
                json={"_challenge": ch, "credential": device.get(ch)})
    return device
