#!/usr/bin/env bash
# scripts/sentinel-demo.sh — make the Sentinel console show something.
#
# Until the control-plane Claude exists (Phase 6) nothing ever asks
# Sentinel for anything, so the console is an empty room with a kill
# switch in it. Run this and a real capability request appears in it,
# waiting for you. Approve it and watch the call complete.
#
# This is the demo, and it is the screenshot.
#
#   scripts/sentinel-demo.sh        # no sudo needed
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO/sentinel"

export SENTINEL_CERT_DIR="${SENTINEL_CERT_DIR:-$PWD/certs}"
export SENTINEL_BROKER_URL="${SENTINEL_BROKER_URL:-https://$(docker network inspect \
  "${K3D_NETWORK:-k3d-devlab}" --format '{{(index .IPAM.Config 0).Gateway}}'):8401}"
# The console URL is whatever this install serves; ask the service.
if curl -sk -o /dev/null --max-time 3 https://localhost:8400/healthz 2>/dev/null; then
  export SENTINEL_CONSOLE_ORIGIN="https://localhost:8400"
else
  export SENTINEL_CONSOLE_ORIGIN="http://localhost:8400"
fi

SVC=$(kubectl get svc -n envoy-gateway-system -o name | grep sentinel-proxy | head -1)
[[ -n "$SVC" ]] || { echo "!! sentinel-proxy data plane not found" >&2; exit 1; }
kubectl port-forward -n envoy-gateway-system "$SVC" 18080:80 >/dev/null 2>&1 &
trap 'kill %1 2>/dev/null || true' EXIT
sleep 3
export SENTINEL_PROXY_URL="http://127.0.0.1:18080"

# -u: this script prints instructions and then WAITS. Buffered output
# would show the human nothing until it was over, which defeats it.
exec .venv/bin/python -u scripts/demo.py "$@"
