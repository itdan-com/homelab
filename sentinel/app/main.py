"""Sentinel admin API — FastAPI application.

TRUST-DOMAIN RULE (CLAUDE.md "Trust-domain separation is
non-negotiable"): this app binds to 127.0.0.1 ONLY. Cluster pods reach
the WSL2 host via the host-gateway IP (172.19.80.1-style), which can
never address the host's loopback — so everything served here (grant
endpoints, GUI, kill switch) is unreachable from inside k3d by
construction, before any auth is even considered. The single
cluster-facing endpoint (/capability-check, for the Envoy ext_authz
proxy) will get its OWN listener with mTLS at 5.5.3/5.5.4 — never
add it to this app without that separation.
"""

from fastapi import FastAPI
from sqlalchemy import text

from . import __version__
from .db import engine

app = FastAPI(title="Sentinel", version=__version__)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness + DB reachability (used by systemd watchdog at 5.5.7)."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "version": __version__, "db": "ok"}
