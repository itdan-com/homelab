#!/usr/bin/env bash
# scripts/sentinel-smoke.sh — Phase 5.5.8 end-to-end battery.
#
# Sits beside sso-dance.sh and netpol-smoke.sh as part of the scripted
# battery. Drives the REAL MCP protocol (official SDK client) through the
# real Sentinel proxy against a real MCP server, and asserts every claim
# the phase makes — including that each of the three layers refuses
# independently.
#
#   sudo scripts/sentinel-smoke.sh            # mints its own enrolment code
#   scripts/sentinel-smoke.sh --enroll-code X # unprivileged, code from
#                                             # sudo sentinel/scripts/enroll-operator.sh
#
# SMOKE_SKIP_TTL=1 skips the one assertion that costs a real 65 seconds.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO/sentinel"

ETC_ENV="/etc/sentinel/sentinel.env"
if [[ -r "$ETC_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$ETC_ENV"
  export SENTINEL_DB SENTINEL_CERT_DIR
  export SENTINEL_ADMIN_URL="http://${SENTINEL_RP_ID}:${SENTINEL_ADMIN_PORT}"
  export SENTINEL_BROKER_URL="https://${SENTINEL_BROKER_BIND}:${SENTINEL_BROKER_PORT}"
else
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
