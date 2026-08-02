#!/usr/bin/env bash
# sentinel/scripts/install-systemd.sh — deploy Sentinel as a service.
#
# ONE install path for every Linux host (ADR-004 construction rule).
# Cloud-init on a DigitalOcean droplet calls this same script; nothing
# below knows or cares that today's host happens to be WSL2. The single
# lab-specific fact — which address the cluster reaches the broker on —
# is DETECTED here and written to /etc/sentinel/sentinel.env, so the
# unit files stay environment-agnostic. In cloud you pass it in:
#
#   SENTINEL_BROKER_BIND=10.1.2.3 sudo -E ./scripts/install-systemd.sh
#
# Idempotent. Re-run it after `git pull` — that IS the deploy step, and
# it is deliberate rather than a git checkout being hot-swapped under a
# running service.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${SENTINEL_APP_DIR:-/opt/sentinel}"
ETC_DIR="${SENTINEL_ETC_DIR:-/etc/sentinel}"
CERT_DIR="${SENTINEL_CERT_DIR:-$ETC_DIR/certs}"
STATE_DIR="${SENTINEL_STATE_DIR:-/var/lib/sentinel}"
SVC_USER="${SENTINEL_USER:-sentinel}"
ADMIN_BIND="${SENTINEL_ADMIN_BIND:-127.0.0.1}"
ADMIN_PORT="${SENTINEL_ADMIN_PORT:-8400}"
BROKER_PORT="${SENTINEL_BROKER_PORT:-8401}"
RP_ID="${SENTINEL_RP_ID:-localhost}"
K3D_NETWORK="${K3D_NETWORK:-k3d-devlab}"
# The door (7.3.3): the person-facing listener. Binds loopback by
# default — the lab reaches it from this host's browser and MCP client;
# a deployment serving real workstations sets SENTINEL_DOOR_BIND=0.0.0.0
# (or a specific interface) and a publicly-resolvable SENTINEL_DOOR_ORIGIN.
DOOR_BIND="${SENTINEL_DOOR_BIND:-127.0.0.1}"
DOOR_PORT="${SENTINEL_DOOR_PORT:-8402}"
DOOR_HOSTNAME="${SENTINEL_DOOR_HOSTNAME:-mcp.lab.local}"
DOOR_ORIGIN="${SENTINEL_DOOR_ORIGIN:-https://localhost:$DOOR_PORT}"
# Which IdP the door federates sign-in to, and how to reach it. The
# ISSUER is the logical identity that must match the `iss` in tokens;
# the HTTP base is where it actually answers on this host (the lab's
# Traefik is on :8443 behind a Host header, cloud has them identical).
OIDC_ISSUER="${SENTINEL_OIDC_ISSUER:-https://authentik.lab.local/application/o/mcp/}"
OIDC_HTTP_BASE="${SENTINEL_OIDC_HTTP_BASE:-https://authentik.lab.local:8443}"
OIDC_CLIENT_ID="${SENTINEL_OIDC_CLIENT_ID:-mcp-door}"
# The lab CA that signed the IdP's certificate. Detected from the
# cluster if present — never hardcoded (ADR-004).
OIDC_CA_BUNDLE="${SENTINEL_OIDC_CA_BUNDLE:-$CERT_DIR/lab-ca.crt}"
MCP_UPSTREAMS="${SENTINEL_MCP_UPSTREAMS:-}"

[[ $EUID -eq 0 ]] || { echo "!! run with sudo" >&2; exit 1; }

# 7.2: the policy store keeps its version history in a local git repo
# (ADR-005 D5 — git as memory). Without git, activation fails and the
# person-path stays closed, so refuse loudly here instead of quietly
# there. Cloud-init installs git before calling this script.
command -v git >/dev/null 2>&1 || {
  echo "!! git is required (the policy store's history is a local git repo)." >&2
  exit 1; }

# The broker's address: given, or detected from the local container
# network. Detection lives HERE and only here.
BROKER_BIND="${SENTINEL_BROKER_BIND:-$(docker network inspect "$K3D_NETWORK" \
  --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)}"
if [[ -z "$BROKER_BIND" ]]; then
  echo "!! could not detect a broker bind address." >&2
  echo "   Set SENTINEL_BROKER_BIND=<address pods reach this host on>." >&2
  exit 1
fi

echo "== service account: $SVC_USER"
id -u "$SVC_USER" >/dev/null 2>&1 || \
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SVC_USER"

echo "== deploying code to $APP_DIR (source: $REPO_DIR)"
install -d -o root -g root -m 0755 "$APP_DIR"
# The app only; never the developer's venv, dev database, or certs.
rm -rf "$APP_DIR/app" "$APP_DIR/migrations"
cp -r "$REPO_DIR/app" "$REPO_DIR/migrations" "$APP_DIR/"
install -m 0644 "$REPO_DIR/alembic.ini" "$REPO_DIR/requirements.txt" "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "== virtualenv"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# Prove the DEPLOYED venv can actually build every app before systemd
# tries. A developer's venv accumulates packages that requirements.txt
# never pinned, so "it runs here" says nothing about a clean install —
# python-multipart went missing exactly this way (2026-08-02), and the
# only symptom was a unit crash-looping with the real error buried in
# journalctl. Importing each app here surfaces it in the install output,
# next to the line that caused it.
echo "== import check (clean-venv proof)"
( cd "$APP_DIR" && SENTINEL_DB="$STATE_DIR/sentinel.db" \
  "$APP_DIR/.venv/bin/python" -c \
  'import app.broker, app.main, app.door' ) || {
  echo "!! the deployed venv cannot build the app — a dependency is missing" >&2
  echo "   from requirements.txt (the error above names it)." >&2
  exit 1; }

echo "== state + config directories"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$STATE_DIR"
install -d -o root -g root -m 0755 "$ETC_DIR"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$CERT_DIR"

# The policy store (7.2, ADR-005 D5). Seeded ONCE from the committed
# example so the console's Access screen lands on something real —
# and never re-seeded: the store belongs to the operator from the
# moment it exists, and an install must not overwrite their policy.
POLICY_DIR="$STATE_DIR/policy"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$POLICY_DIR"
if [[ ! -f "$POLICY_DIR/entities.yaml" ]]; then
  for f in entities.yaml matrix.yaml servers.yaml overlay.cedar; do
    install -o "$SVC_USER" -g "$SVC_USER" -m 0600 \
      "$REPO_DIR/policy-example/$f" "$POLICY_DIR/$f"
  done
  echo "== policy store seeded from policy-example/ (edit it on the console)"
fi

# Certificates: reuse what already exists so the CA — and therefore the
# cluster's trust in it — survives a reinstall. Only mint when there is
# nothing to keep.
# Copy any cert this install is missing. Per-file, not all-or-nothing:
# the first version only copied when ca.crt was absent, so a re-install
# after minting a NEW leaf (the console cert) silently kept the old set
# and the service started against material that did not exist.
copied=0
for f in ca.crt ca.key broker.crt broker.key proxy-client.crt proxy-client.key \
         console.crt console.key door.crt door.key; do
  if [[ ! -f "$CERT_DIR/$f" && -f "$REPO_DIR/certs/$f" ]]; then
    cp "$REPO_DIR/certs/$f" "$CERT_DIR/$f"; copied=$((copied + 1))
  fi
done
if (( copied )); then
  echo "== adopted $copied cert file(s) from the repo checkout (existing CA preserved)"
  chown -R "$SVC_USER:$SVC_USER" "$CERT_DIR"; chmod 0600 "$CERT_DIR"/*
fi
if [[ ! -f "$CERT_DIR/ca.crt" || ! -f "$CERT_DIR/console.crt" \
      || ! -f "$CERT_DIR/door.crt" ]]; then
  echo "!! missing certificates (need ca.crt, console.crt and door.crt)." >&2
  echo "   Run ./scripts/mint-certs.sh as the host user, then re-run this." >&2
  exit 1
fi

# The door validates the IdP's TLS against the CLUSTER's CA, which is a
# different trust root from Sentinel's own (deliberately — Sentinel
# never trusts something the cluster can mint for its own material).
# Detected from the live cluster; an install without a cluster leaves
# the door to fall back to the system trust store, which is correct in
# cloud where the IdP has a real certificate.
if [[ ! -f "$CERT_DIR/lab-ca.crt" ]] && command -v kubectl >/dev/null 2>&1; then
  if kubectl -n cert-manager get secret lab-local-ca -o jsonpath='{.data.tls\.crt}' \
       2>/dev/null | base64 -d > "$CERT_DIR/lab-ca.crt.tmp" \
     && [[ -s "$CERT_DIR/lab-ca.crt.tmp" ]]; then
    mv "$CERT_DIR/lab-ca.crt.tmp" "$CERT_DIR/lab-ca.crt"
    chown "$SVC_USER:$SVC_USER" "$CERT_DIR/lab-ca.crt"
    chmod 0644 "$CERT_DIR/lab-ca.crt"
    echo "== adopted the cluster CA for IdP verification"
  else
    rm -f "$CERT_DIR/lab-ca.crt.tmp"
  fi
fi
[[ -f "$OIDC_CA_BUNDLE" ]] || OIDC_CA_BUNDLE=""

ENV_FILE="$ETC_DIR/sentinel.env"
if [[ -f "$ENV_FILE" ]]; then
  cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"   # never clobber config unbacked
  echo "== existing $ENV_FILE backed up"
fi
cat > "$ENV_FILE" <<EOF
# Generated by install-systemd.sh — the ONE place host-specific values
# live. The unit files read this and know nothing about the host.
SENTINEL_DB=$STATE_DIR/sentinel.db
SENTINEL_CERT_DIR=$CERT_DIR
SENTINEL_ADMIN_BIND=$ADMIN_BIND
SENTINEL_ADMIN_PORT=$ADMIN_PORT
SENTINEL_BROKER_BIND=$BROKER_BIND
SENTINEL_BROKER_PORT=$BROKER_PORT
SENTINEL_RP_ID=$RP_ID
SENTINEL_CONSOLE_ORIGIN=https://$RP_ID:$ADMIN_PORT
SENTINEL_CONSOLE_HOSTS=127.0.0.1,localhost
SENTINEL_POLICY_DIR=$STATE_DIR/policy
# --- the door (7.3.3): the person-facing listener ---
SENTINEL_DOOR_BIND=$DOOR_BIND
SENTINEL_DOOR_PORT=$DOOR_PORT
SENTINEL_DOOR_ORIGIN=$DOOR_ORIGIN
SENTINEL_DOOR_KEY=$STATE_DIR/door-signing-key.pem
SENTINEL_OIDC_ISSUER=$OIDC_ISSUER
SENTINEL_OIDC_HTTP_BASE=$OIDC_HTTP_BASE
SENTINEL_OIDC_CLIENT_ID=$OIDC_CLIENT_ID
SENTINEL_OIDC_CA_BUNDLE=$OIDC_CA_BUNDLE
# Where each MCP server actually lives (name=url,name=url). Empty until
# a real server is deployed — policy can decide about a server that has
# no upstream yet; it simply cannot be called.
SENTINEL_MCP_UPSTREAMS=$MCP_UPSTREAMS
EOF
chmod 0640 "$ENV_FILE"; chown root:"$SVC_USER" "$ENV_FILE"

# Trust Sentinel's CA for the Windows user, so the console is a valid
# https origin in Edge/Chrome with no manual import. Deliberately
# `-user`, not the machine store: no elevation, and the blast radius is
# one account. Firefox keeps its OWN store and is not covered here.
# Undo:  certutil.exe -user -delstore Root "Sentinel CA"
if [[ -z "${SENTINEL_SKIP_CA_TRUST:-}" ]]; then
  # Under sudo, root's PATH carries no Windows interop — `command -v
  # certutil.exe` fails and this block used to skip SILENTLY. That
  # skip meant the CA was never actually trusted at 5.5.7, masked for
  # days by the http-era console (2026-08-02 finding). Full-path
  # fallback first; if genuinely unavailable, say so loudly — on a
  # pure Linux host that line is expected and harmless.
  CERTUTIL="$(command -v certutil.exe || true)"
  [[ -z "$CERTUTIL" && -x /mnt/c/Windows/System32/certutil.exe ]] && \
    CERTUTIL=/mnt/c/Windows/System32/certutil.exe
  if [[ -n "$CERTUTIL" ]]; then
    WINTMP="/mnt/c/Users/Public/sentinel-ca.crt"
    if cp "$CERT_DIR/ca.crt" "$WINTMP" 2>/dev/null; then
      if "$CERTUTIL" -user -addstore Root 'C:\Users\Public\sentinel-ca.crt' >/dev/null 2>&1; then
        echo "== Sentinel CA trusted for the Windows user (Edge/Chrome)."
        echo "   undo: certutil.exe -user -delstore Root \"Sentinel CA\""
      else
        echo "!! could not trust the CA automatically — import $CERT_DIR/ca.crt by hand"
      fi
      rm -f "$WINTMP"
    else
      echo "!! could not stage the CA on /mnt/c — import $CERT_DIR/ca.crt by hand"
    fi
  else
    echo "== note: certutil.exe not reachable — Windows CA trust NOT updated."
    echo "   From a normal WSL shell: cp $CERT_DIR/ca.crt /mnt/c/Users/Public/ &&"
    echo "   certutil.exe -user -addstore Root 'C:\\Users\\Public\\ca.crt'"
  fi
fi

echo "== schema"
# Snapshot before migrating — a system-state rewrite gets a same-script
# backup, always. Keep the last three, rotate the rest.
DB_FILE="$STATE_DIR/sentinel.db"
if [[ -f "$DB_FILE" ]]; then
  cp -a "$DB_FILE" "$DB_FILE.pre-migrate.$(date +%Y%m%d%H%M%S)"
  ls -1t "$DB_FILE".pre-migrate.* 2>/dev/null | tail -n +4 | xargs -r rm --
  echo "== database snapshot taken (keeping the last 3)"
fi
SENTINEL_DB="$STATE_DIR/sentinel.db" \
  runuser -u "$SVC_USER" -- "$APP_DIR/.venv/bin/alembic" \
  -c "$APP_DIR/alembic.ini" upgrade head

echo "== units"
install -m 0644 "$REPO_DIR/deploy/sentinel-admin.service" \
                "$REPO_DIR/deploy/sentinel-broker.service" \
                "$REPO_DIR/deploy/sentinel-door.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sentinel-broker.service sentinel-admin.service \
                 sentinel-door.service
# RESTART, never `enable --now`: --now starts a stopped unit and
# silently leaves a running one on the OLD code — which is exactly how
# the 2026-07-27 https fix to the admin unit sat unapplied for five
# days while `status` reported active. A deploy that does not restart
# is not a deploy.
systemctl restart sentinel-broker.service sentinel-admin.service \
                  sentinel-door.service

# Probe the WIRE, not the unit state: "active" told the truth all five
# of those days while the transport was wrong. The install now proves
# each listener answers on the scheme production expects — strict
# verification against Sentinel's own CA (the SANs cover localhost and
# the broker address) — or it fails right here, loudly.
sleep 2
ADMIN_OK=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  --cacert "$CERT_DIR/ca.crt" "https://$RP_ID:$ADMIN_PORT/healthz" || true)
[[ "$ADMIN_OK" == "200" ]] || {
  echo "!! admin console did not answer https on :$ADMIN_PORT (got '${ADMIN_OK}')" >&2
  echo "   journalctl -u sentinel-admin -n 50" >&2
  exit 1; }
BROKER_OK=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  --cacert "$CERT_DIR/ca.crt" \
  --cert "$CERT_DIR/proxy-client.crt" --key "$CERT_DIR/proxy-client.key" \
  "https://$BROKER_BIND:$BROKER_PORT/healthz" || true)
[[ "$BROKER_OK" == "200" ]] || {
  echo "!! broker did not answer mTLS https on $BROKER_BIND:$BROKER_PORT (got '${BROKER_OK}')" >&2
  echo "   journalctl -u sentinel-broker -n 50" >&2
  exit 1; }
DOOR_OK=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  --cacert "$CERT_DIR/ca.crt" "https://localhost:$DOOR_PORT/healthz" || true)
[[ "$DOOR_OK" == "200" ]] || {
  echo "!! door did not answer https on :$DOOR_PORT (got '${DOOR_OK}')" >&2
  echo "   journalctl -u sentinel-door -n 50" >&2
  exit 1; }
# The door must serve MCP discovery, and its self-described resource
# must match the origin people will actually type — a metadata document
# advertising an unreachable address is worse than none.
DOOR_RESOURCE=$(curl -s --max-time 5 --cacert "$CERT_DIR/ca.crt" \
  "https://localhost:$DOOR_PORT/.well-known/oauth-protected-resource" \
  | sed -n 's/.*"resource":"\([^"]*\)".*/\1/p')
[[ "$DOOR_RESOURCE" == "$DOOR_ORIGIN/mcp" ]] || {
  echo "!! door advertises resource '${DOOR_RESOURCE}', expected $DOOR_ORIGIN/mcp" >&2
  exit 1; }
# An unauthenticated MCP call must be refused AND must point the client
# at the sign-in metadata. This one line is the whole "birthright needs
# an identity" property, checked on the wire.
DOOR_401=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST \
  --cacert "$CERT_DIR/ca.crt" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  "https://localhost:$DOOR_PORT/mcp" || true)
[[ "$DOOR_401" == "401" ]] || {
  echo "!! unauthenticated MCP call returned '${DOOR_401}', expected 401" >&2
  exit 1; }
echo "== wire probes: admin https 200, broker mTLS 200, door https 200 " \
     "(discovery ok, unauthenticated call refused)"
# The two POLICY CONSUMERS must agree on the active version. They are
# separate processes sharing only the store on disk (7.3.1), so a
# silent disagreement means two different answers to the same question.
# (The admin console is the store's WRITER and its /healthz stays
# deliberately uninformative — its version lives behind the passkey at
# /v1/policy/status.)
BROKER_PV=$(curl -s --max-time 5 --cacert "$CERT_DIR/ca.crt" \
  --cert "$CERT_DIR/proxy-client.crt" --key "$CERT_DIR/proxy-client.key" \
  "https://$BROKER_BIND:$BROKER_PORT/healthz" \
  | sed -n 's/.*"policy_version": *"\([^"]*\)".*/\1/p')
DOOR_PV=$(curl -s --max-time 5 --cacert "$CERT_DIR/ca.crt" \
  "https://localhost:$DOOR_PORT/healthz" \
  | sed -n 's/.*"policy_version": *"\([^"]*\)".*/\1/p')
if [[ -z "$DOOR_PV" || -z "$BROKER_PV" ]]; then
  echo "!! a policy consumer has NO active policy (broker='${BROKER_PV:-none}'" \
       "door='${DOOR_PV:-none}') — nothing is reachable through it" >&2
  echo "   check $STATE_DIR/policy and journalctl -u sentinel-door" >&2
  exit 1
elif [[ "$BROKER_PV" != "$DOOR_PV" ]]; then
  echo "!! policy version DISAGREEMENT: broker='$BROKER_PV' door='$DOOR_PV'" >&2
  exit 1
fi
echo "== policy version agreed by broker and door: $DOOR_PV"
systemctl --no-pager --lines=0 status sentinel-broker.service \
  sentinel-admin.service sentinel-door.service || true

# First install: mint an enrollment code and hand over one clickable
# URL (the code rides the fragment — never sent to the server, never in
# access logs). RE-install with operators already enrolled: no code, no
# banner telling an enrolled human to enroll — their passkey keeps
# working because the DB, the RP ID, and the certificate all survived.
OPERATORS=$(runuser -u "$SVC_USER" -- env "SENTINEL_DB=$STATE_DIR/sentinel.db" \
  "$APP_DIR/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$APP_DIR')
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models import Operator
with SessionLocal() as s:
    print(s.scalar(select(func.count()).select_from(Operator)) or 0)")

if [[ "$OPERATORS" -gt 0 ]]; then
  cat <<EOF

  ============================================================
   Sentinel updated. $OPERATORS operator(s) already enrolled —
   log in as usual:

     https://$RP_ID:$ADMIN_PORT/
  ============================================================

  Add a second device:  sudo $REPO_DIR/scripts/enroll-operator.sh
  logs:  journalctl -u sentinel-admin -u sentinel-broker -f

EOF
else
  ENROLL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo operator)}"
  CODE=$(runuser -u "$SVC_USER" -- env "SENTINEL_DB=$STATE_DIR/sentinel.db" \
    "$APP_DIR/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$APP_DIR')
from app.auth import mint_enrollment_code
from app.db import SessionLocal
s = SessionLocal()
print(mint_enrollment_code(s, '$ENROLL_USER', 'first device'))")

  cat <<EOF

  ============================================================
   Sentinel is running. Open this once and register a passkey:

     https://$RP_ID:$ADMIN_PORT/#enroll=$CODE

   That is the whole setup. The link expires in 10 minutes.
  ============================================================

  Add a second device later (do it — there is no recovery backdoor):
    sudo $REPO_DIR/scripts/enroll-operator.sh

  logs:  journalctl -u sentinel-admin -u sentinel-broker -f

EOF
fi
