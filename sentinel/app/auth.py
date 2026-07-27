"""Human authentication for the console (5.5.6).

The rule from `CLAUDE.md`: **never password-only** — phishing the
password phishes the kill switch. So the primary factor is a WebAuthn
passkey, which cannot be phished at all: the authenticator signs a
challenge bound to the origin, so a lookalike site gets a signature
that verifies against nothing.

Three decisions worth stating, because each one closes a hole that the
obvious implementation leaves open:

1. **Registration needs an out-of-band code.** "No credential exists
   yet, so let the first browser register" is the common shortcut and
   it means anything that can reach the port — a local process, or a
   page that talks the operator's browser into a request — can enroll
   itself as the approver. `scripts/enroll-operator.sh` mints a code on
   the host and prints it to the terminal, so enrolling requires
   already having the host. The same path adds a second device later:
   one mechanism, no special case for "first run".

2. **Challenges live server-side and are single-use.** A challenge the
   client keeps is a challenge the client can replay.

3. **Sessions are server-side rows, not self-contained signed cookies.**
   Revocation has to be real: a stolen laptop is answered by deleting
   rows, not by waiting out an expiry the attacker's copy also carries.

TOTP is the documented fallback for when no authenticator is available.
It is deliberately second: a TOTP seed is a shared secret that can be
phished, which is exactly what the passkey avoids. It can only be
enrolled by an already-authenticated operator.
"""

import hashlib
import secrets
from datetime import timedelta

import pyotp
import webauthn
from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import (
    CONSOLE_ORIGIN,
    CONSOLE_RP_ID,
    ENROLLMENT_TTL_MINUTES,
    SESSION_TTL_MINUTES,
)
from .models import (
    AuditEventType,
    ConsoleSession,
    EnrollmentCode,
    Operator,
    WebAuthnChallenge,
    WebAuthnCredential,
    utcnow,
)
from .service import audit

RP_NAME = "Sentinel"
SESSION_COOKIE = "sentinel_session"
CHALLENGE_TTL_SECONDS = 300


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- enrollment codes (minted on the host, never over the network) -----------

def mint_enrollment_code(s: Session, username: str, label: str) -> str:
    code = secrets.token_urlsafe(24)
    s.add(EnrollmentCode(
        code_hash=_hash(code), username=username, label=label,
        expires_at=utcnow() + timedelta(minutes=ENROLLMENT_TTL_MINUTES),
    ))
    audit(s, AuditEventType.CREDENTIAL_ADDED, actor=username,
          details={"stage": "code-minted", "label": label})
    s.commit()
    return code


def _live_enrollment(s: Session, code: str) -> EnrollmentCode | None:
    row = s.scalars(
        select(EnrollmentCode).where(EnrollmentCode.code_hash == _hash(code))
    ).first()
    if row is None or row.used_at is not None or utcnow() >= row.expires_at:
        return None
    return row


# --- challenges ---------------------------------------------------------------

def _new_challenge(s: Session, purpose: str, *, operator_id=None,
                   enrollment_id=None) -> bytes:
    raw = secrets.token_bytes(32)
    s.add(WebAuthnChallenge(
        challenge=raw, purpose=purpose, operator_id=operator_id,
        enrollment_id=enrollment_id,
        expires_at=utcnow() + timedelta(seconds=CHALLENGE_TTL_SECONDS),
    ))
    s.commit()
    return raw


def _consume_challenge(s: Session, purpose: str, raw: bytes) -> WebAuthnChallenge | None:
    """Single use: found, deleted, then verified. A challenge that
    survives its ceremony is a replay waiting to happen."""
    row = s.scalars(
        select(WebAuthnChallenge).where(
            WebAuthnChallenge.challenge == raw,
            WebAuthnChallenge.purpose == purpose,
        )
    ).first()
    if row is None:
        return None
    expired = utcnow() >= row.expires_at
    detached = WebAuthnChallenge(
        challenge=row.challenge, purpose=row.purpose,
        operator_id=row.operator_id, enrollment_id=row.enrollment_id,
        expires_at=row.expires_at,
    )
    s.delete(row)
    s.commit()
    return None if expired else detached


# --- registration -------------------------------------------------------------

def begin_registration(s: Session, code: str) -> dict | None:
    enrollment = _live_enrollment(s, code)
    if enrollment is None:
        return None
    operator = s.scalars(
        select(Operator).where(Operator.username == enrollment.username)
    ).first()
    existing = []
    if operator is not None:
        existing = [
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in s.scalars(select(WebAuthnCredential).where(
                WebAuthnCredential.operator_id == operator.id)).all()
        ]

    challenge = _new_challenge(s, "register", enrollment_id=enrollment.id)
    options = webauthn.generate_registration_options(
        rp_id=CONSOLE_RP_ID,
        rp_name=RP_NAME,
        user_name=enrollment.username,
        user_id=(operator.id if operator else enrollment.username).encode(),
        challenge=challenge,
        # Discoverable + user-verified: the operator proves presence AND
        # identity (PIN/biometric) on the authenticator itself, so a
        # stolen key alone is not enough.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        # Already-registered authenticators are excluded so the browser
        # says "you already have one" instead of silently duplicating.
        exclude_credentials=existing,
    )
    return {"options": webauthn.options_to_json(options),
            "username": enrollment.username, "label": enrollment.label}


def finish_registration(s: Session, credential: dict) -> Operator | None:
    raw_challenge = base64url_to_bytes(
        credential.get("_challenge", "")) if credential.get("_challenge") else None
    if raw_challenge is None:
        return None
    pending = _consume_challenge(s, "register", raw_challenge)
    if pending is None:
        return None
    enrollment = s.get(EnrollmentCode, pending.enrollment_id)
    if enrollment is None or enrollment.used_at is not None:
        return None

    try:
        verified = webauthn.verify_registration_response(
            credential=credential["credential"],
            expected_challenge=raw_challenge,
            expected_rp_id=CONSOLE_RP_ID,
            expected_origin=CONSOLE_ORIGIN,
            require_user_verification=True,
        )
    except Exception as exc:
        audit(s, AuditEventType.AUTH_FAILURE, actor=enrollment.username,
              details={"stage": "register", "error": type(exc).__name__})
        s.commit()
        return None

    operator = s.scalars(
        select(Operator).where(Operator.username == enrollment.username)
    ).first()
    if operator is None:
        operator = Operator(username=enrollment.username)
        s.add(operator)
        s.flush()

    s.add(WebAuthnCredential(
        operator_id=operator.id,
        label=enrollment.label,
        credential_id=webauthn.helpers.bytes_to_base64url(verified.credential_id),
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
    ))
    enrollment.used_at = utcnow()
    audit(s, AuditEventType.CREDENTIAL_ADDED, actor=operator.username,
          details={"stage": "registered", "label": enrollment.label})
    s.commit()
    return operator


# --- authentication -----------------------------------------------------------

def begin_login(s: Session) -> str:
    challenge = _new_challenge(s, "login")
    options = webauthn.generate_authentication_options(
        rp_id=CONSOLE_RP_ID,
        challenge=challenge,
        # No allow_credentials: discoverable credentials let the browser
        # offer the right passkey without the console first asking "who
        # are you", which would leak whether a username exists.
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return webauthn.options_to_json(options)


def finish_login(s: Session, credential: dict) -> tuple[Operator, str] | None:
    raw_challenge = base64url_to_bytes(
        credential.get("_challenge", "")) if credential.get("_challenge") else None
    if raw_challenge is None or _consume_challenge(s, "login", raw_challenge) is None:
        return None

    presented = credential["credential"]
    cred_row = s.scalars(select(WebAuthnCredential).where(
        WebAuthnCredential.credential_id == presented["id"])).first()
    if cred_row is None:
        audit(s, AuditEventType.AUTH_FAILURE,
              details={"stage": "login", "error": "unknown-credential"})
        s.commit()
        return None

    try:
        verified = webauthn.verify_authentication_response(
            credential=presented,
            expected_challenge=raw_challenge,
            expected_rp_id=CONSOLE_RP_ID,
            expected_origin=CONSOLE_ORIGIN,
            credential_public_key=cred_row.public_key,
            credential_current_sign_count=cred_row.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        audit(s, AuditEventType.AUTH_FAILURE, actor=cred_row.operator.username,
              details={"stage": "login", "error": type(exc).__name__})
        s.commit()
        return None

    cred_row.sign_count = verified.new_sign_count
    cred_row.last_used_at = utcnow()
    return cred_row.operator, _open_session(s, cred_row.operator, "webauthn")


# --- TOTP fallback ------------------------------------------------------------

def totp_provisioning_uri(s: Session, operator: Operator) -> str:
    """Enrol (or re-enrol) the fallback. Only an already-authenticated
    operator can reach this — the fallback is a convenience for the
    person who already proved who they are, never a way in."""
    operator.totp_secret = pyotp.random_base32()
    operator.totp_confirmed_at = None
    s.commit()
    return pyotp.TOTP(operator.totp_secret).provisioning_uri(
        name=operator.username, issuer_name=RP_NAME)


def confirm_totp(s: Session, operator: Operator, code: str) -> bool:
    if not operator.totp_secret:
        return False
    if not pyotp.TOTP(operator.totp_secret).verify(code, valid_window=1):
        audit(s, AuditEventType.AUTH_FAILURE, actor=operator.username,
              details={"stage": "totp-confirm"})
        s.commit()
        return False
    operator.totp_confirmed_at = utcnow()
    audit(s, AuditEventType.CREDENTIAL_ADDED, actor=operator.username,
          details={"stage": "totp-confirmed"})
    s.commit()
    return True


def login_with_totp(s: Session, username: str, code: str) -> tuple[Operator, str] | None:
    operator = s.scalars(select(Operator).where(Operator.username == username)).first()
    if (operator is None or not operator.totp_secret
            or operator.totp_confirmed_at is None
            or not pyotp.TOTP(operator.totp_secret).verify(code, valid_window=1)):
        audit(s, AuditEventType.AUTH_FAILURE, actor=username,
              details={"stage": "totp-login"})
        s.commit()
        return None
    return operator, _open_session(s, operator, "totp")


# --- sessions -----------------------------------------------------------------

def _open_session(s: Session, operator: Operator, method: str) -> str:
    token = secrets.token_urlsafe(32)
    s.add(ConsoleSession(
        operator_id=operator.id, token_hash=_hash(token), method=method,
        expires_at=utcnow() + timedelta(minutes=SESSION_TTL_MINUTES),
    ))
    audit(s, AuditEventType.AUTH_SUCCESS, actor=operator.username,
          details={"method": method})
    s.commit()
    return token


def session_operator(s: Session, token: str | None) -> Operator | None:
    if not token:
        return None
    row = s.scalars(
        select(ConsoleSession).where(ConsoleSession.token_hash == _hash(token))
    ).first()
    if row is None or row.revoked_at is not None or utcnow() >= row.expires_at:
        return None
    return row.operator


def close_session(s: Session, token: str | None) -> None:
    if not token:
        return
    row = s.scalars(
        select(ConsoleSession).where(ConsoleSession.token_hash == _hash(token))
    ).first()
    if row is not None and row.revoked_at is None:
        row.revoked_at = utcnow()
        s.commit()


def operator_count(s: Session) -> int:
    return len(s.scalars(select(Operator)).all())
