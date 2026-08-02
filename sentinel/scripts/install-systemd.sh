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
         console.crt console.key; do
  if [[ ! -f "$CERT_DIR/$f" && -f "$REPO_DIR/certs/$f" ]]; then
    cp "$REPO_DIR/certs/$f" "$CERT_DIR/$f"; copied=$((copied + 1))
  fi
done
if (( copied )); then
  echo "== adopted $copied cert file(s) from the repo checkout (existing CA preserved)"
  chown -R "$SVC_USER:$SVC_USER" "$CERT_DIR"; chmod 0600 "$CERT_DIR"/*
fi
if [[ ! -f "$CERT_DIR/ca.crt" || ! -f "$CERT_DIR/console.crt" ]]; then
  echo "!! missing certificates (need ca.crt and console.crt)." >&2
  echo "   Run ./scripts/mint-certs.sh as the host user, then re-run this." >&2
  exit 1
fi

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
                "$REPO_DIR/deploy/sentinel-broker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sentinel-broker.service sentinel-admin.service
# RESTART, never `enable --now`: --now starts a stopped unit and
# silently leaves a running one on the OLD code — which is exactly how
# the 2026-07-27 https fix to the admin unit sat unapplied for five
# days while `status` reported active. A deploy that does not restart
# is not a deploy.
systemctl restart sentinel-broker.service sentinel-admin.service

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
echo "== wire probes: admin https 200, broker mTLS 200"
systemctl --no-pager --lines=0 status sentinel-broker.service sentinel-admin.service || true

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
