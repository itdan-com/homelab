"""Environment-driven settings.

Kept deliberately tiny: Sentinel is a trust anchor, and every knob is
attack surface. Add settings only when a checklist item needs them.
"""

import getpass
import os

# SQLite database path. Dev default is repo-local (gitignored);
# production (5.5.7) sets SENTINEL_DB=/var/lib/sentinel/sentinel.db
# via the systemd unit.
DB_PATH = os.environ.get("SENTINEL_DB", "./sentinel-dev.db")
DB_URL = f"sqlite+pysqlite:///{DB_PATH}"

# How long a pending request waits for a human before lapsing.
REQUEST_TTL_MINUTES = int(os.environ.get("SENTINEL_REQUEST_TTL_MINUTES", "10"))

# Default grant lifetime when the granter doesn't choose one (the GUI's
# buttons send 5 or 60 explicitly).
DEFAULT_GRANT_TTL_MINUTES = int(os.environ.get("SENTINEL_GRANT_TTL_MINUTES", "5"))

# Hard ceiling on any single grant. Was 24 hours purely because that is
# a round number; the console only ever offers 5 and 60, so the extra
# 1380 minutes existed solely for something that got past the console.
MAX_GRANT_TTL_MINUTES = int(os.environ.get("SENTINEL_MAX_GRANT_TTL_MINUTES", "60"))

# Token prefix: makes credentials grep-able in logs/incident response
# (the industry "sk_"-style convention) without weakening entropy.
TOKEN_PREFIX = "snt_"

# Who the human at the console IS. Every grant/deny/kill is attributed
# to this identity — and it comes from the SERVER, never from the
# request body: an actor a caller can type is a signature anyone can
# forge. Today it is config (the host has one operator); at 5.5.6
# app.actor resolves it from the verified WebAuthn credential instead
# and nothing else changes.
OPERATOR = os.environ.get("SENTINEL_OPERATOR") or getpass.getuser()

# Hostnames the admin console may be addressed by. This is the
# anti-DNS-rebinding control: a browser pointed at evil.com whose DNS
# resolves to 127.0.0.1 still sends `Host: evil.com`, and is refused.
# Cloud (ADR-002) replaces this with the real console hostname.
CONSOLE_ALLOWED_HOSTS = [
    h.strip() for h in
    os.environ.get("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost").split(",")
    if h.strip()
]

# --- human auth (5.5.6) ------------------------------------------------------
#
# WebAuthn's Relying Party ID must be a DOMAIN — the spec does not allow
# a bare IP, so the console is reached at localhost:8400 and never
# 127.0.0.1:8400.
#
# And it is served over **https**, even on loopback. Not for
# eavesdroppers — there are none here — but because browser passkey
# providers (1Password, Dashlane, …) decline to engage on a plain-http
# origin and silently fall through to the platform authenticator, which
# then fails. "http://localhost is a secure context by spec" is true and
# not sufficient: what matters is what the extension will touch. Found
# 2026-07-27, by bisect — the same browser and extension enrolled
# happily on an https site and offered nothing on ours. In cloud this
# becomes the real console hostname over a real certificate (ADR-004),
# so this is the production shape rather than a local workaround.
CONSOLE_RP_ID = os.environ.get("SENTINEL_RP_ID", "localhost")
CONSOLE_PORT = int(os.environ.get("SENTINEL_ADMIN_PORT", "8400"))
CONSOLE_ORIGIN = os.environ.get(
    "SENTINEL_CONSOLE_ORIGIN", f"http://{CONSOLE_RP_ID}:{CONSOLE_PORT}")

# How long a console session lives before the human must present the
# authenticator again. Short on purpose: this session can revoke every
# capability on the platform.
SESSION_TTL_MINUTES = int(os.environ.get("SENTINEL_SESSION_TTL_MINUTES", "60"))

# An enrollment code is the out-of-band secret that authorizes adding a
# NEW authenticator. It is minted by `scripts/enroll-operator.sh` on the
# host and printed to the terminal — so registering a passkey requires
# shell access to the Sentinel host, not merely the ability to reach the
# console's port.
ENROLLMENT_TTL_MINUTES = int(os.environ.get("SENTINEL_ENROLLMENT_TTL_MINUTES", "10"))

# A flow counts as active if it holds a live grant, or if anything at
# all happened on it within this window. (`ended_at` is only set when a
# caller explicitly closes a flow, which nothing does yet — so it can
# never be the sole activity signal.)
FLOW_ACTIVE_MINUTES = int(os.environ.get("SENTINEL_FLOW_ACTIVE_MINUTES", "15"))
