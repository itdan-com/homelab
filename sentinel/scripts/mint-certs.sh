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
CONSOLE_HOSTNAME="${SENTINEL_RP_ID:-localhost}"
CONSOLE_ALT_HOSTNAME="${SENTINEL_CONSOLE_ALT_HOSTNAME:-sentinel.lab.local}"
DOOR_HOSTNAME="${SENTINEL_DOOR_HOSTNAME:-mcp.lab.local}"
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
[[ "$ROTATE" == 1 ]] && rm -f broker.crt broker.key proxy-client.crt proxy-client.key \
                                console.crt console.key door.crt door.key

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
    # Expiry-aware: re-running this script on day 91 used to print a
    # success-shaped "keeping it" while every MCP call failed closed
    # behind an expired cert. Renew inside 30 days, automatically.
    if openssl x509 -in "$name.crt" -noout -checkend $((30 * 86400)) >/dev/null; then
      echo "== $name.crt valid — keeping it ($(openssl x509 -in "$name.crt" -noout -enddate))"
      return
    fi
    if openssl x509 -in "$name.crt" -noout -checkend 0 >/dev/null; then
      echo "!! $name.crt expires within 30 days — renewing now"
    else
      echo "!! $name.crt HAS ALREADY EXPIRED — renewing now (traffic was failing closed)"
    fi
    rm -f "$name.crt" "$name.key"
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

# The CONSOLE's own server certificate. The admin console is served over
# TLS even on loopback, and not for eavesdroppers — there are none on
# loopback. It is because browser passkey providers (1Password, Dashlane,
# …) decline to engage on a plain-http origin and silently fall through
# to the platform authenticator, which is how "http://localhost works,
# it is a secure context by spec" turns into an unusable enrolment
# screen. Proven 2026-07-27: the same browser + extension enrolled
# happily on an https site and offered nothing on our http one.
# Serving https is also the posture cloud needs anyway (ADR-004), so
# this is the production shape rather than a workaround.
#
# Two names on purpose: `localhost` is the default Relying Party ID, and
# a hosts-file name is the fallback if a provider special-cases
# localhost — switching is then one env var, not a re-mint.
mint_leaf console "$CONSOLE_HOSTNAME" "basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,DNS:$CONSOLE_ALT_HOSTNAME,IP:127.0.0.1"

# The DOOR's server certificate (7.3.3). Unlike the console this one is
# meant to be reached by other machines — people's workstations — so it
# carries the door hostname alongside the loopback names the lab uses.
# In cloud this leaf is replaced by a publicly-trusted certificate for
# `mcp.<domain>` (ADR-002): an employee's laptop will not have our CA,
# and telling people to install a root CA to use a tool is how you train
# a workforce to click through certificate warnings.
mint_leaf door "$DOOR_HOSTNAME" "basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$DOOR_HOSTNAME,DNS:localhost,IP:127.0.0.1"

for leaf in broker console door; do
  echo "== SANs on $leaf.crt:"
  openssl x509 -in "$leaf.crt" -noout -ext subjectAltName | sed 's/^/   /'
done

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
