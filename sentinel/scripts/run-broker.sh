#!/usr/bin/env bash
# sentinel/scripts/run-broker.sh — boot the cluster-facing broker with
# mTLS REQUIRED (the 5.5.7 systemd unit's ExecStart will mirror this).
#
# Binds the k3d network gateway address (the WSL2 VM as pods see it) —
# detected, not hardcoded — and serves TLS from the Sentinel CA's
# broker cert. `--ssl-cert-reqs 2` is ssl.CERT_REQUIRED: a peer that
# does not present a Sentinel-issued client certificate cannot even
# complete the handshake, let alone speak to a route. The admin app
# (app.main) stays loopback-only and plain HTTP — loopback IS its
# boundary until WebAuthn lands at 5.5.6.
set -euo pipefail
cd "$(dirname "$0")/.."

CERT_DIR="${SENTINEL_CERT_DIR:-$PWD/certs}"
K3D_NETWORK="${K3D_NETWORK:-k3d-devlab}"
PORT="${SENTINEL_BROKER_PORT:-8401}"
BIND="${SENTINEL_BROKER_BIND:-$(docker network inspect "$K3D_NETWORK" \
  --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)}"

if [[ -z "$BIND" ]]; then
  echo "!! could not detect the $K3D_NETWORK gateway IP and SENTINEL_BROKER_BIND is unset" >&2
  exit 1
fi
for f in broker.crt broker.key ca.crt; do
  [[ -f "$CERT_DIR/$f" ]] || { echo "!! $CERT_DIR/$f missing — run scripts/mint-certs.sh first" >&2; exit 1; }
done

echo "== broker on https://$BIND:$PORT (mTLS required, CA=$CERT_DIR/ca.crt)"
exec .venv/bin/uvicorn app.broker:app --host "$BIND" --port "$PORT" \
  --ssl-certfile "$CERT_DIR/broker.crt" \
  --ssl-keyfile "$CERT_DIR/broker.key" \
  --ssl-ca-certs "$CERT_DIR/ca.crt" \
  --ssl-cert-reqs 2
