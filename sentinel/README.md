# Sentinel — the trust-domain-separated capability broker

Sentinel is the security backbone of Phase 5.5: a small service that
runs **on the WSL2 host as a systemd unit — deliberately OUTSIDE the
k3d cluster**. It mints short-lived, scope-locked capability tokens
(default 5 min, bound to one MCP tool + one flow-id), shows every
request to a human in a one-screen GUI (Grant 5m / Grant 1h / Deny),
holds the **global kill switch**, and writes the canonical audit log.

The architecture contract (see `CLAUDE.md` → "Trust-domain separation"
and `docs/phases/phase-05-5-sentinel.md`):

- **One-way trust.** The cluster has NO path to Sentinel's admin
  surface — no kubectl route, no NetworkPolicy, no service account.
  Claude can *request* and *observe denials*; only the human *grants*.
- **Bind discipline enforces it at layer 3, mTLS at layer 5.** The
  admin API + GUI bind to `127.0.0.1` only. Cluster pods reach the
  WSL2 host via gateway IPs, which cannot address the host's loopback —
  so the admin surface is unreachable from any pod *by construction*.
  The cluster-facing broker listener additionally requires a client
  certificate from **Sentinel's own CA** (5.5.4; `scripts/mint-certs.sh`,
  deliberately not cert-manager — the cluster must never be able to
  mint a cert the broker trusts). The sentinel-proxy's Envoy fleet
  holds the one client identity; nothing else in the cluster does.
- **Not a catalog chart, ever.** Sentinel must survive the cluster's
  deletion — it lives here as plain code deployed by systemd (5.5.7),
  not by ArgoCD.

## Two listeners (the trust boundary, made physical)

Sentinel runs as **two ASGI apps**, and which one a route lives on IS
the security model — not a convention that can be forgotten:

| App | Binds | Who reaches it | Routes |
|---|---|---|---|
| `app.broker` | k3d gateway IP, **mTLS required** | cluster pods (via the Sentinel proxy) | request a capability, poll for the answer, **check** a token, **/v1/ext-authz** (Envoy's per-call question) |
| `app.main` (admin) | `127.0.0.1` | the human at the console (`http://localhost:8400/`, passkey required) | **grant**, **deny**, **kill**/release, audit, flows |

### Signing in (5.5.6)

The console is shut until a **WebAuthn passkey** is verified — every
route that reads or changes platform state answers 401 without a
session. Reads are gated too: the pending panel and the audit log
describe what the platform's agent is trying to do and what it has
done, which is not public merely because it is not a button.

A passkey rather than a password because `CLAUDE.md` says never
password-only, and a passkey cannot be phished at all: the
authenticator signs a challenge bound to *this origin*, so a lookalike
site gets a signature that verifies against nothing.

**Enrolling requires shell access to this host**, not merely the
ability to reach the port:

```bash
sentinel/scripts/enroll-operator.sh          # prints a single-use code
# then open http://localhost:8400/ and paste it
```

`localhost`, not `127.0.0.1` — WebAuthn's Relying Party ID must be a
domain, and an IP is not one. Run the same command again to add a
second device: **that is the recovery story.** There is no account
recovery backdoor, because a backdoor is a second front door to the
kill switch. TOTP exists as the documented fallback and can only be
enrolled by an operator who is already signed in.

### The console is a web page, so loopback is not the whole story

Binding to loopback stops the *cluster*. It does not stop the
operator's own browser, which will carry a request from any tab to
loopback. Three independent controls sit in front of every
state-changing route, *below* the authentication above — they stop a
page the operator visits from driving the console even while a valid
session exists:

1. **Host allowlist** — defeats DNS rebinding (`Host: evil.com` → 400).
2. **Origin check** — defeats plain cross-site requests (→ 403).
3. **Console header** — `X-Sentinel-Console: 1` on every POST. A
   cross-origin page cannot set a custom header without a CORS
   preflight, and this app answers none. **curl users must send it too.**

The page itself loads nothing from anywhere (`default-src 'none'` CSP,
no CDN) and renders every agent-written string — tool names, reasons,
flow ids — with `textContent`, never as markup: the one screen holding
the kill switch is the last place to accept markup from the thing
asking for power.

**Identity is resolved by the server, never asserted by the caller.**
There is no `granted_by` field to send; the audit log records the
operator whose passkey opened the session — a cryptographically
established human, not a string someone typed.

Granting has no route on the broker app. A pod cannot reach loopback
via the gateway IP, so the grant/kill surface is unreachable from k3d
by construction — *before* the passkey is even asked for. Layer 3 and
layer 7 answer independently.

## The capability loop

```
Claude ──POST /v1/capability-requests──▶ broker      (202, request_id)
          {…, claim_nonce: <secret you mint and keep>}
Claude ──GET  …/{id}  X-Claim-Nonce ──▶ broker      (pending…)
                     human ──POST …/{id}/grant──▶ admin   (mints token)
Claude ──GET  …/{id}  X-Claim-Nonce ──▶ broker      (granted + token, ONCE)
proxy  ──POST /v1/ext-authz/<orig path>▶ broker      (200 forward / 403 refuse)
```

The last line is Envoy's `ext_authz` callout (`catalog/sentinel-proxy`):
the original path is appended to the prefix and the original BODY rides
along, so the broker derives the tool being exercised itself —
`<server>.<params.name>` for `tools/call`, `<server>.rpc.<method>`
otherwise (`app/scope.py`). A caller cannot name one tool and invoke
another, on any server. `GET /v1/capability-check?token&tool&flow_id`
remains for humans and scripts.

Token delivery is **claim-once, and it belongs to the caller that
asked**: the requester mints a `claim_nonce`, Sentinel stores only its
hash, and the plaintext token is handed over on the first post-grant
poll that presents the matching nonce — nowhere else. Without that, any
caller could name someone else's flow and tool, be handed their
`request_id` by the dedupe path, and race them to the pickup the
instant the human clicked Grant. A wrong nonce gets the same 404 as an
unknown id, so it cannot even confirm the request exists.

The human who grants never sees the token, and Sentinel keeps only a
SHA-256 hash. `/capability-check` answers with **HTTP status** (200
allow, 403 deny) — the contract Envoy's `ext_authz` speaks directly —
and takes the token as the `X-Sentinel-Token` **header**, never a query
parameter: uvicorn's access log records full query strings, so a token
in the URL would sit in journald in plaintext long after the grant died.

## Dev quickstart (on the WSL2 host)

```bash
cd sentinel
python3 -m venv .venv                        # needs the python3.12-venv apt pkg
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head               # create/upgrade the SQLite schema

# admin (human) console + interactive docs on loopback:
.venv/bin/uvicorn app.main:app --reload --port 8400
#   → http://localhost:8400/              the one-screen console
#   → http://localhost:8400/openapi.json  the generated API schema
# (localhost, not 127.0.0.1: WebAuthn's RP ID must be a domain.)
# (Swagger UI is deliberately OFF: it loads its JavaScript from a public
#  CDN, and the origin that owns the kill switch executes no third-party
#  code. app/schemas.py's Field descriptions are the reference text, and
#  they are generated from the code, so they cannot drift.)

# once per install: mint Sentinel's CA + broker/client certs and inject
# the cluster-side ConfigMap/Secret (rotation: re-run with --rotate):
scripts/mint-certs.sh

# broker (cluster-facing, mTLS REQUIRED), separate process:
scripts/run-broker.sh
```

Tests (dev deps in `requirements-dev.txt`):

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/            # 20-assertion lifecycle
```

`SENTINEL_DB` overrides the SQLite path (dev default:
`./sentinel-dev.db`, gitignored; production lands in
`/var/lib/sentinel/` at 5.5.7). `SENTINEL_REQUEST_TTL_MINUTES` (10)
and `SENTINEL_GRANT_TTL_MINUTES` (5) tune the default lifetimes.

## Layout

```
sentinel/
├── app/
│   ├── main.py        # ADMIN listener: console + grant/deny/kill/audit
│   ├── broker.py      # CLUSTER listener: request/poll/check/ext-authz (mTLS)
│   ├── service.py     # every state transition + its audit, in one place
│   ├── scope.py       # request → capability-scope derivation (pure, tested)
│   ├── actor.py       # who is acting (from the session) + the CSRF guard
│   ├── auth.py        # WebAuthn ceremonies, sessions, TOTP fallback
│   ├── auth_routes.py # /auth/* endpoints the console calls
│   ├── console/       # the one-screen GUI (no build step, no CDN)
│   ├── schemas.py     # Pydantic shapes = the /docs human documentation
│   ├── config.py      # env-driven settings (DB path/URL, TTLs, operator)
│   ├── db.py          # SQLAlchemy engine/session/Base
│   └── models.py      # data model (flows, grants, requests, audit, kill)
├── scripts/
│   ├── mint-certs.sh  # Sentinel CA + broker/client certs + cluster inject
│   ├── enroll-operator.sh # mint a single-use code to add an authenticator
│   └── run-broker.sh  # boot the broker with client-cert-required TLS
├── certs/             # minted material (gitignored; /var/lib at 5.5.7)
├── migrations/        # Alembic (env.py wired to app.models metadata)
├── alembic.ini
├── pyproject.toml     # project metadata + top-level deps
└── requirements.txt   # frozen pins (reproducible installs)
```

Stack decision (5.5.1): **Python 3.12 + FastAPI + SQLAlchemy/Alembic
on SQLite**, `py_webauthn` planned for 5.5.6. Chosen over Node/Fastify
for WebAuthn library maturity, Alembic's boring-and-proven SQLite
migrations, and zero new toolchain on the host (python3 + venv were
already there). Boring is a feature in the trust anchor.
