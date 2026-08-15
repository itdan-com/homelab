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
    "SENTINEL_CONSOLE_ORIGIN", f"https://{CONSOLE_RP_ID}:{CONSOLE_PORT}")

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

# --- the policy store (7.2.2, ADR-005 D5) ------------------------------------
#
# Where the console-authored authorization documents live (entity
# store, access matrix, servers, overlay) plus their generated Cedar
# and the local git history. Dev default is repo-local and GITIGNORED
# (it grows its own .git); production is /var/lib/sentinel/policy via
# the systemd unit — same construction rule as SENTINEL_DB.
POLICY_DIR = os.environ.get("SENTINEL_POLICY_DIR", "./policy-dev")

# 7.3.1 — how often each process re-checks the store on disk for a
# version it has not loaded (seconds; 0 disables the watcher, for
# tests). The console activates policy in the ADMIN process; the
# BROKER is a separate process whose only shared state with it is the
# store on disk — so each process watches the store and does a
# READ-ONLY rebuild when the bytes change. This value is the bound on
# how long the two processes may disagree about the active version
# after a console save.
POLICY_RELOAD_SECONDS = float(os.environ.get("SENTINEL_POLICY_RELOAD_SECONDS", "2"))

# --- velocity (ADR-007 Decision 1) --------------------------------------------
#
# How many times a principal has used a tool, on a given tier, inside
# each trailing window — computed synchronously by the broker from its
# own audit log immediately before every Cedar evaluation, and exposed
# as context.actions_in_window so a policy can write e.g.
# `forbid ... when { context.actions_in_window._1m > 20 }`. The window
# BUCKETS here are a schema shape (they become field names on the Cedar
# context record — app/policy.py's schema is generated from this dict's
# keys, so the two can never drift), not a per-deployment tuning knob:
# changing them means updating the schema and any policy that
# references them, so they are a constant, not an env var. The
# THRESHOLD a policy compares against is exactly what's tunable, per
# policy, without touching code.
VELOCITY_WINDOWS_MINUTES = {"_1m": 1, "_1h": 60}

# --- the record (7.6) --------------------------------------------------------
#
# How long audit rows stay in the database before being exported to a
# sealed JSONL segment and removed. 90 days is the owner's number and a
# common compliance floor; the segments are kept indefinitely (they are
# small, line-oriented, and are what a SIEM or Loki reads).
AUDIT_RETAIN_DAYS = int(os.environ.get("SENTINEL_AUDIT_RETAIN_DAYS", "90"))
AUDIT_EXPORT_DIR = os.environ.get(
    "SENTINEL_AUDIT_EXPORT_DIR",
    os.path.join(os.path.dirname(DB_PATH) or ".", "audit-segments"))
# How often the admin process seals new rows into the hash chain.
# Exactly ONE process seals — two would fork the chain.
AUDIT_SEAL_SECONDS = float(os.environ.get("SENTINEL_AUDIT_SEAL_SECONDS", "30"))

# --- the Airlock door (7.3.3) -------------------------------------------------
#
# A THIRD listener, because Sentinel now faces three populations with
# three trust levels: the cluster (broker, mTLS), the human operator
# (admin console, loopback + passkey), and now PEOPLE AT WORKSTATIONS
# with MCP clients. The door is the only one meant to be reachable by
# an ordinary signed-in employee, so it holds no admin capability and
# never binds the console's port.
DOOR_BIND = os.environ.get("SENTINEL_DOOR_BIND", "127.0.0.1")
DOOR_PORT = int(os.environ.get("SENTINEL_DOOR_PORT", "8402"))

# The door's PUBLIC origin — what clients type and what every issued
# document must self-describe as. OAuth metadata that advertises an
# address the client cannot reach is worse than none; and the `iss`
# and `aud` claims are derived from this, so a mismatch is a security
# bug, not a cosmetic one. Cloud (ADR-002) sets https://mcp.<domain>.
DOOR_ORIGIN = os.environ.get("SENTINEL_DOOR_ORIGIN",
                             f"https://localhost:{DOOR_PORT}")

# Where the door sends people to prove WHO they are. Authentik is the
# shipped IdP (ADR-005 D9 amendment): it authenticates; it does not
# authorize — that is the policy store's job alone.
OIDC_ISSUER = os.environ.get(
    "SENTINEL_OIDC_ISSUER", "https://authentik.lab.local/application/o/mcp/")
OIDC_CLIENT_ID = os.environ.get("SENTINEL_OIDC_CLIENT_ID", "mcp-door")
OIDC_CLIENT_SECRET = os.environ.get("SENTINEL_OIDC_CLIENT_SECRET") or None

# Lab-parity wart, honestly named: the issuer is a logical identity
# (https://authentik.lab.local/...) while the reachable address here is
# localhost:8443 behind a Host header. This override rewrites the
# host:port of fetched endpoint URLs for TRANSPORT only — `iss` stays
# the logical issuer for validation. In cloud the two are identical and
# this is empty, which is exactly ADR-004's rule: the host-specific
# value is detected at install and written to config, never baked in.
OIDC_HTTP_BASE = os.environ.get("SENTINEL_OIDC_HTTP_BASE") or None
OIDC_CA_BUNDLE = os.environ.get("SENTINEL_OIDC_CA_BUNDLE") or None

# The door signs its own person-tokens (RS256). Key lives beside the
# database in the state dir, 0600, generated on first start.
DOOR_KEY_PATH = os.environ.get(
    "SENTINEL_DOOR_KEY", os.path.join(os.path.dirname(DB_PATH) or ".",
                                      "door-signing-key.pem"))
DOOR_TOKEN_TTL_MINUTES = int(
    os.environ.get("SENTINEL_DOOR_TOKEN_TTL_MINUTES", "480"))

# Static client allowlist (comma-separated client_ids) for MCP clients
# that do not publish a CIMD document. CIMD is the preferred path and
# DCR is refused permanently (owner, 2026-08-02): unauthenticated
# self-registration is the deprecated, insecure branch of the spec.
DOOR_STATIC_CLIENTS = [c.strip() for c in
                       os.environ.get("SENTINEL_DOOR_STATIC_CLIENTS", "").split(",")
                       if c.strip()]

# Where each MCP server actually lives: `name=url,name=url`. This is
# CONFIG, not policy, on purpose — the policy store says WHETHER a
# person may call a server, config says WHERE that server is. Keeping
# the URL out of the console means a policy edit can never retarget
# traffic to an attacker's host; the two authorities stay separate.
# A server with no upstream here is decidable but not callable, which
# is the honest state until 7.4 deploys the first real one.
# Upstream CREDENTIALS, kept in Sentinel's trust domain rather than in
# the workload (7.4). GitHub's MCP server in http mode has no static
# token at all — verified against the shipped v1.8.0 binary, whose
# ServerConfig has no token field and which 401s every request lacking
# an `Authorization` header (GitHub's own changelog claims a static
# fallback; it does not exist, and upstream issue #2946 reports the
# same). Every caller must present a per-request credential, so the
# credential belongs to whoever authorizes the call — us.
#
# The security consequence is a genuine improvement: the MCP server pod
# holds NO credential, so compromising it steals nothing. Format is
# {"<server>": "<token>"} in a root-owned file the service can read but
# not write.
# Lives in the STATE dir, service-owned — like the policy store, and for
# the same reason. The "a trust anchor must not rewrite its own config"
# rule is about the UNIT's configuration (/etc/sentinel/sentinel.env,
# root-owned): what Sentinel is and how it binds. Upstream credentials
# are operational data the operator manages day to day, and putting them
# where only root could write them meant the console could not offer the
# one thing an operator actually wants — paste it and hit save. Same
# gate as every other console write: a passkey, and an audit row.
MCP_UPSTREAM_TOKENS_FILE = os.environ.get(
    "SENTINEL_MCP_UPSTREAM_TOKENS",
    os.path.join(os.path.dirname(DB_PATH) or ".", "upstream-credentials.json"))


def upstream_token(server: str, caller: str | None = None) -> str | None:
    """Read at call time, not import time: rotating a credential should
    be editing one file, never restarting the trust anchor. App-backed
    entries mint a short-lived token here (app.upstream_auth), so a
    production deployment never has a 90-day rotation chore."""
    from .upstream_auth import token_for
    return token_for(server, MCP_UPSTREAM_TOKENS_FILE, caller=caller)


# Where this platform's own MCP servers answer. A connection that says
# "runs on this platform" resolves to <base>/<server>/mcp — the
# operator registers a server and the address is DERIVED, because
# making someone type the address of a thing this platform deployed
# itself is a chore the platform invented for them.
MCP_PROXY_BASE = os.environ.get(
    "SENTINEL_MCP_PROXY_BASE", "https://localhost:8443").rstrip("/")

MCP_UPSTREAMS = {
    k.strip(): v.strip() for k, _, v in
    (part.partition("=") for part in
     os.environ.get("SENTINEL_MCP_UPSTREAMS", "").split(",") if part.strip())
    if k.strip() and v.strip()
}
