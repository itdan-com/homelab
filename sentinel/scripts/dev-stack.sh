#!/usr/bin/env bash
# sentinel/scripts/dev-stack.sh — a full Sentinel that needs no root.
#
# WHY THIS EXISTS: the installed units are root-owned system services,
# so every code change was costing the owner a `sudo` deploy. Over a
# build that ran for days that is dozens of interruptions, and the
# person paying that cost was never the one making the change.
#
# This runs the same code as the real thing — broker, admin and door —
# under the developer's own account, on its own ports, against its own
# database and policy store. Nothing here can touch the installed
# units, their state, or their certificates: different ports, different
# paths, different everything. When a change is proven here, the owner
# deploys ONCE.
#
#   ./scripts/dev-stack.sh up      start (or restart) the dev stack
#   ./scripts/dev-stack.sh down    stop it
#   ./scripts/dev-stack.sh status  what is listening, and its policy version
#
# What it deliberately cannot prove, so nobody mistakes it for a full
# rehearsal: the systemd units themselves, the passkey console (a real
# authenticator lives on a real origin), the installed CA trust, and
# the owner's actual policy store. Those still need the real install —
# which is exactly why the install line should be rare and deliberate.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN="${SENTINEL_DEV_DIR:-${TMPDIR:-/tmp}/sentinel-dev}"
ADMIN_PORT="${SENTINEL_DEV_ADMIN_PORT:-8410}"
BROKER_PORT="${SENTINEL_DEV_BROKER_PORT:-8411}"
DOOR_PORT="${SENTINEL_DEV_DOOR_PORT:-8412}"

export SENTINEL_DB="$RUN/sentinel-dev.db"
export SENTINEL_POLICY_DIR="$RUN/policy"
export SENTINEL_MCP_UPSTREAM_TOKENS="$RUN/upstream-credentials.json"
export SENTINEL_DOOR_KEY="$RUN/door-key.pem"
export SENTINEL_ADMIN_PORT="$ADMIN_PORT"
export SENTINEL_DOOR_PORT="$DOOR_PORT"
export SENTINEL_DOOR_ORIGIN="https://localhost:$DOOR_PORT"
export SENTINEL_CONSOLE_ORIGIN="https://localhost:$ADMIN_PORT"
export SENTINEL_CONSOLE_HOSTS="127.0.0.1,localhost"
# Point at the live IdP and cluster: those are read-only from here, and
# testing against a stub is how integration bugs survive to production.
# DASH defaults, not COLON-DASH (`${V-x}` vs `${V:-x}`): an explicitly
# EMPTY export must survive — "no transport rewrite" and "system trust
# store" are legitimate values for an external IdP, and `:-` silently
# replaced them with the Authentik lab defaults, sending an
# Okta-pointed door's discovery to authentik.lab.local (found live,
# 2026-08-23 — the same trap the 7.8.1 review caught in the installer).
export SENTINEL_OIDC_ISSUER="${SENTINEL_OIDC_ISSUER-https://authentik.lab.local/application/o/mcp/}"
export SENTINEL_OIDC_HTTP_BASE="${SENTINEL_OIDC_HTTP_BASE-https://authentik.lab.local:8443}"
export SENTINEL_OIDC_CLIENT_ID="${SENTINEL_OIDC_CLIENT_ID-mcp-door}"
export SENTINEL_OIDC_CA_BUNDLE="${SENTINEL_OIDC_CA_BUNDLE-$RUN/lab-ca.crt}"
export SENTINEL_MCP_PROXY_BASE="${SENTINEL_MCP_PROXY_BASE:-https://localhost:8443}"

up() {
  mkdir -p "$RUN/policy"
  # Seed a store from the committed example — never a copy of the
  # owner's live policy, which is theirs and must not be read or
  # written by a dev rig.
  for f in entities.yaml matrix.yaml servers.yaml overlay.cedar; do
    [[ -f "$RUN/policy/$f" ]] || cp "$DIR/policy-example/$f" "$RUN/policy/$f"
  done
  # The cluster CA, for verifying the IdP and the proxy. Read-only.
  [[ -f "$RUN/lab-ca.crt" ]] || kubectl -n cert-manager get secret lab-local-ca \
    -o jsonpath='{.data.tls\.crt}' 2>/dev/null | base64 -d > "$RUN/lab-ca.crt" || true
  [[ -s "$RUN/lab-ca.crt" ]] || { rm -f "$RUN/lab-ca.crt"; unset SENTINEL_OIDC_CA_BUNDLE; }
  [[ -f "$DIR/certs/door.crt" ]] || { echo "!! run ./scripts/mint-certs.sh first" >&2; exit 1; }

  down >/dev/null 2>&1 || true
  "$DIR/.venv/bin/alembic" -c "$DIR/alembic.ini" upgrade head >/dev/null

  ( cd "$DIR" && nohup "$DIR/.venv/bin/uvicorn" app.door:app \
      --host 127.0.0.1 --port "$DOOR_PORT" \
      --ssl-certfile certs/door.crt --ssl-keyfile certs/door.key \
      > "$RUN/door.log" 2>&1 & echo $! > "$RUN/door.pid" )
  ( cd "$DIR" && nohup "$DIR/.venv/bin/uvicorn" app.main:app \
      --host 127.0.0.1 --port "$ADMIN_PORT" \
      --ssl-certfile certs/console.crt --ssl-keyfile certs/console.key \
      > "$RUN/admin.log" 2>&1 & echo $! > "$RUN/admin.pid" )
  sleep 3
  status
}

down() {
  for p in door admin broker; do
    [[ -f "$RUN/$p.pid" ]] && kill "$(cat "$RUN/$p.pid")" 2>/dev/null || true
    rm -f "$RUN/$p.pid"
  done
  echo "dev stack stopped"
}

status() {
  printf 'door   https://localhost:%s  ' "$DOOR_PORT"
  curl -s --cacert "$DIR/certs/ca.crt" --max-time 3 \
    "https://localhost:$DOOR_PORT/healthz" || echo "(down)"
  printf '\nadmin  https://localhost:%s  ' "$ADMIN_PORT"
  curl -s --cacert "$DIR/certs/ca.crt" --max-time 3 \
    "https://localhost:$ADMIN_PORT/healthz" || echo "(down)"
  printf '\nstate  %s\n' "$RUN"
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) echo "usage: $0 [up|down|status]" >&2; exit 2 ;;
esac
