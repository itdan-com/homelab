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

## Dev quickstart (on the WSL2 host)

```bash
cd sentinel
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head          # create/upgrade the SQLite schema
.venv/bin/uvicorn app.main:app --reload # admin API on 127.0.0.1:8000 (dev)
curl -s http://127.0.0.1:8000/healthz
```

`SENTINEL_DB` overrides the SQLite path (dev default:
`./sentinel-dev.db`, gitignored; production lands in
`/var/lib/sentinel/` at 5.5.7).

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
