#!/usr/bin/env bash
# scripts/sentinel-smoke.sh — Phase 5.5.8 end-to-end battery.
#
# Sits beside sso-dance.sh and netpol-smoke.sh in the scripted battery.
# Drives the REAL MCP protocol (official SDK client) through the real
# Sentinel proxy against a real MCP server, and asserts every claim the
# phase makes — including that each of the three layers refuses
# independently.
#
#   sudo scripts/sentinel-smoke.sh
#
# It needs root because the production database is root-only by design.
# That means it also has to put back the two things sudo takes away:
# the invoking user's kubeconfig, and their environment.
#
# SMOKE_SKIP_TTL=1 skips the one assertion that costs a real 65 seconds.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO/sentinel"

# sudo resets HOME to /root, so kubectl looks for a kubeconfig that is
# not there and reports the wonderfully unhelpful "the server could not
# find the requested resource". Point it back at the invoking user's.
if [[ -n "${SUDO_USER:-}" && -z "${KUBECONFIG:-}" ]]; then
  CALLER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
  if [[ -r "$CALLER_HOME/.kube/config" ]]; then
    export KUBECONFIG="$CALLER_HOME/.kube/config"
  fi
fi
kubectl version -o json >/dev/null 2>&1 || {
  echo "!! kubectl cannot reach the cluster." >&2
  echo "   Set KUBECONFIG to a config root can read, e.g." >&2
  echo "   sudo KUBECONFIG=\$HOME/.kube/config scripts/sentinel-smoke.sh" >&2
  exit 1; }

ETC_ENV="/etc/sentinel/sentinel.env"
if [[ -r "$ETC_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$ETC_ENV"
  # Export everything the battery and app.config both read, so the
  # software authenticator signs for the SAME origin the service
  # verifies against — http or https, whichever this install runs.
  export SENTINEL_DB SENTINEL_CERT_DIR SENTINEL_RP_ID SENTINEL_CONSOLE_ORIGIN
  export SENTINEL_ADMIN_URL="$SENTINEL_CONSOLE_ORIGIN"
  export SENTINEL_BROKER_URL="https://${SENTINEL_BROKER_BIND}:${SENTINEL_BROKER_PORT}"
else
  echo "== no systemd install found; using the dev checkout"
  export SENTINEL_CERT_DIR="${SENTINEL_CERT_DIR:-$PWD/certs}"
  export SENTINEL_BROKER_URL="${SENTINEL_BROKER_URL:-https://$(docker network inspect \
    "${K3D_NETWORK:-k3d-devlab}" --format '{{(index .IPAM.Config 0).Gateway}}'):8401}"
fi

# The proxy is a ClusterIP service; forward it so the battery can drive
# it from the host. The authz path is identical — Envoy runs ext_authz on
# every request either way — and the in-cluster hop is already covered by
# netpol-smoke.sh.
SVC=$(kubectl get svc -n envoy-gateway-system -o name | grep sentinel-proxy | head -1)
[[ -n "$SVC" ]] || { echo "!! sentinel-proxy data plane not found" >&2; exit 1; }
kubectl port-forward -n envoy-gateway-system "$SVC" 18080:80 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
sleep 3
export SENTINEL_PROXY_URL="http://127.0.0.1:18080"

exec .venv/bin/python scripts/smoke-e2e.py "$@"
