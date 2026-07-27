#!/usr/bin/env bash
# sentinel/scripts/mint-certs.sh — Sentinel's OWN private CA and the
# broker's mTLS material.
#
# WHY A SEPARATE CA (not the cluster's lab-local CA): Sentinel is the
# trust anchor and must never depend on anything the cluster can mint.
# cert-manager lives INSIDE the cluster; if a compromised cluster could
# issue certs the broker trusts, the one-way trust model is dead. So
# Sentinel runs its own tiny PKI on the host:
#
#   ca.crt / ca.key            the Sentinel CA (2 years; key never leaves host)
#   broker.crt / broker.key    server cert for the broker listener
#                              (SAN: sentinel-broker.internal + gateway IP)
#   proxy-client.crt / .key    CLIENT cert the in-cluster Envoy presents;
#                              holding a Sentinel-issued client cert is
#                              the price of talking to the broker AT ALL
#
# Leaf lifetime is 90 days (phase-doc decision). Rotation = re-run with
# --rotate (new leaves, same CA — nothing else to update), then restart
# the broker. --rotate-ca re-mints everything (clients must re-inject).
#
# The script also injects the CLUSTER-side artifacts (idempotently,
# mirroring bootstrap.sh's out-of-git age-key Secret pattern):
#   ConfigMap sentinel-ca           (ca.crt — BackendTLSPolicy validates
#                                    the broker against it)
#   Secret    sentinel-proxy-client (tls type — EnvoyProxy presents it)
# Neither ever touches git: they are per-install material, like the age
# key. Skip with --no-cluster (e.g. minting before a cluster exists).
#
# Everything is env-overridable for adopters; the k3d gateway IP is
# DETECTED from the docker network, never hardcoded.
set -euo pipefail

SENTINEL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CERT_DIR="${SENTINEL_CERT_DIR:-$SENTINEL_DIR/certs}"
BROKER_NAME="${SENTINEL_BROKER_HOSTNAME:-sentinel-broker.internal}"
K3D_NETWORK="${K3D_NETWORK:-k3d-devlab}"
NAMESPACE="${SENTINEL_PROXY_NAMESPACE:-mcp-servers}"
CA_DAYS="${SENTINEL_CA_DAYS:-730}"
LEAF_DAYS="${SENTINEL_LEAF_DAYS:-90}"

ROTATE=0 ROTATE_CA=0 CLUSTER=1
for arg in "$@"; do
  case "$arg" in
    --rotate) ROTATE=1 ;;
    --rotate-ca) ROTATE=1 ROTATE_CA=1 ;;
    --no-cluster) CLUSTER=0 ;;
    *) echo "usage: $0 [--rotate|--rotate-ca] [--no-cluster]" >&2; exit 2 ;;
  esac
done

BROKER_IP="${SENTINEL_BROKER_IP:-$(docker network inspect "$K3D_NETWORK" \
  --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)}"
if [[ -z "$BROKER_IP" ]]; then
  echo "!! could not detect the $K3D_NETWORK gateway IP and SENTINEL_BROKER_IP is unset" >&2
  exit 1
fi

umask 077   # every key lands 0600 from birth
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

[[ "$ROTATE_CA" == 1 ]] && rm -f ca.crt ca.key
[[ "$ROTATE" == 1 ]] && rm -f broker.crt broker.key proxy-client.crt proxy-client.key

# --- CA (ECDSA P-256, matching the lab CA's curve choice) --------------------
if [[ -f ca.crt ]]; then
  echo "== Sentinel CA exists — keeping it ($(openssl x509 -in ca.crt -noout -enddate))"
else
  echo "== minting Sentinel CA ($CA_DAYS days)"
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days "$CA_DAYS" \
    -subj "/CN=Sentinel CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out ca.crt
fi

mint_leaf() { # name, CN, extensions
  local name="$1" cn="$2" ext="$3"
  if [[ -f "$name.crt" ]]; then
    echo "== $name.crt exists — keeping it ($(openssl x509 -in "$name.crt" -noout -enddate))"
    return
  fi
  echo "== minting $name ($LEAF_DAYS days, CN=$cn)"
  openssl ecparam -name prime256v1 -genkey -noout -out "$name.key"
  openssl req -new -key "$name.key" -subj "/CN=$cn" -out "$name.csr"
  openssl x509 -req -in "$name.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days "$LEAF_DAYS" -sha256 -extfile <(printf '%s\n' "$ext") -out "$name.crt"
  rm -f "$name.csr"
  openssl verify -CAfile ca.crt "$name.crt" >/dev/null
}

mint_leaf broker "$BROKER_NAME" "basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=DNS:$BROKER_NAME,IP:$BROKER_IP"

mint_leaf proxy-client sentinel-proxy "basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth"

echo "== SANs on broker.crt:"
openssl x509 -in broker.crt -noout -ext subjectAltName | sed 's/^/   /'

# --- cluster-side artifacts (out-of-git, idempotent) -------------------------
if [[ "$CLUSTER" == 1 ]]; then
  echo "== injecting cluster artifacts into namespace $NAMESPACE"
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
  kubectl create configmap sentinel-ca -n "$NAMESPACE" \
    --from-file=ca.crt=ca.crt \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl create secret tls sentinel-proxy-client -n "$NAMESPACE" \
    --cert=proxy-client.crt --key=proxy-client.key \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "== --no-cluster: skipped ConfigMap/Secret injection"
fi

echo "== done. certs in $CERT_DIR (gitignored)."
