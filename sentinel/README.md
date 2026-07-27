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
- **Bind discipline enforces it at layer 3.** The admin API + GUI bind
  to `127.0.0.1` only. Cluster pods reach the WSL2 host via the
  host-gateway IP (e.g. `172.19.80.1`), which cannot address the
  host's loopback — so the admin surface is unreachable from any pod
  *by construction*. The one cluster-facing endpoint
  (`GET /capability-check`, called by the Envoy ext_authz proxy) gets
  its own listener + mTLS when it lands (5.5.3/5.5.4).
- **Not a catalog chart, ever.** Sentinel must survive the cluster's
  deletion — it lives here as plain code deployed by systemd (5.5.7),
  not by ArgoCD.

## Two listeners (the trust boundary, made physical)

Sentinel runs as **two ASGI apps**, and which one a route lives on IS
the security model — not a convention that can be forgotten:

| App | Binds | Who reaches it | Routes |
|---|---|---|---|
| `app.broker` | k3d gateway IP | cluster pods (via the Sentinel proxy) | request a capability, poll for the answer, **check** a token |
| `app.main` (admin) | `127.0.0.1` | the human at the console | **grant**, **deny**, **kill**/release, audit, flows |

Granting has no route on the broker app. A pod cannot reach loopback
via the gateway IP, so the grant/kill surface is unreachable from k3d
by construction — before any auth exists (WebAuthn/TOTP arrive at
5.5.6; until then loopback-reachability *is* the boundary).

## The capability loop

```
Claude ──POST /v1/capability-requests──▶ broker      (202, request_id)
Claude ──GET  …/{id}  (poll)──────────▶ broker      (pending…)
                     human ──POST …/{id}/grant──▶ admin   (mints token)
Claude ──GET  …/{id}  (poll)──────────▶ broker      (granted + token, ONCE)
proxy  ──GET  /v1/capability-check─────▶ broker      (200 allow / 403 deny)
```

Token delivery is **claim-once**: the plaintext reaches the requester
on its first post-grant poll and nowhere else — the human who grants
never sees it, and Sentinel keeps only a SHA-256 hash. `/capability-check`
answers with **HTTP status** (200 allow, 403 deny) — the contract
Envoy's `ext_authz` speaks directly (5.5.4).

## Dev quickstart (on the WSL2 host)

```bash
cd sentinel
python3 -m venv .venv                        # needs the python3.12-venv apt pkg
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head               # create/upgrade the SQLite schema

# admin (human) API + interactive docs on loopback:
.venv/bin/uvicorn app.main:app --reload --port 8400
#   → http://127.0.0.1:8400/docs

# broker (cluster-facing) API, separate process:
.venv/bin/uvicorn app.broker:app --port 8401 --host 0.0.0.0
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
│   ├── main.py        # FastAPI app + /healthz
│   ├── config.py      # env-driven settings (DB path/URL)
│   ├── db.py          # SQLAlchemy engine/session/Base
│   └── models.py      # data model (flows, capability_grants, audit_events — 5.5.2)
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
