#!/usr/bin/env bash
# sentinel/scripts/enroll-operator.sh — authorize a new authenticator.
#
# Registering a passkey requires a code minted HERE, on the host, and
# printed to this terminal. That is the point: "no credential exists
# yet, so let the first browser register" would mean anything able to
# reach the console's port — a local process, or a page that talks the
# operator's browser into a request — could enroll itself as the human
# who approves. Requiring a code from the host's shell makes enrolling a
# deliberate act by someone who already has the host.
#
# The same command adds a second device later (a phone, a hardware key).
# Do that: a second passkey is the recovery story. There is no account
# recovery backdoor, because a backdoor is a second front door to the
# kill switch.
#
# Works against whichever Sentinel is installed:
#   * a systemd install  (/etc/sentinel/sentinel.env present) — needs sudo
#   * a dev checkout     (falls back to the repo venv + dev database)
#
#   ./scripts/enroll-operator.sh                 # default user, default label
#   ./scripts/enroll-operator.sh bob "yubikey"   # explicit user + label
set -euo pipefail
cd "$(dirname "$0")/.."

ETC_ENV="${SENTINEL_ETC_DIR:-/etc/sentinel}/sentinel.env"
SVC_USER="${SENTINEL_USER:-sentinel}"

if [[ -r "$ETC_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$ETC_ENV"
  APP_DIR="${SENTINEL_APP_DIR:-/opt/sentinel}"
  PY="$APP_DIR/.venv/bin/python"
  MODE="systemd install"
  RUN=(runuser -u "$SVC_USER" -- env "SENTINEL_DB=$SENTINEL_DB" "$PY")
  [[ $EUID -eq 0 ]] || RUN=(sudo "${RUN[@]}")
else
  APP_DIR="$PWD"
  PY="$PWD/.venv/bin/python"
  MODE="dev checkout"
  RUN=(env "SENTINEL_DB=${SENTINEL_DB:-$PWD/sentinel-dev.db}" "$PY")
  SENTINEL_RP_ID="${SENTINEL_RP_ID:-localhost}"
  SENTINEL_ADMIN_PORT="${SENTINEL_ADMIN_PORT:-8400}"
fi

USERNAME="${1:-${SENTINEL_OPERATOR:-$(logname 2>/dev/null || id -un)}}"
LABEL="${2:-$(hostname) $(date +%Y-%m-%d 2>/dev/null || echo device)}"

CODE=$("${RUN[@]}" - "$APP_DIR" "$USERNAME" "$LABEL" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from app.auth import mint_enrollment_code
from app.db import SessionLocal

with SessionLocal() as s:
    print(mint_enrollment_code(s, sys.argv[2], sys.argv[3]))
PY
)

cat <<EOF

  ($MODE)  Enrollment code for '$USERNAME' ($LABEL):

      $CODE

  1. Open  http://${SENTINEL_RP_ID}:${SENTINEL_ADMIN_PORT}/  in a browser ON THIS HOST.
     (The hostname matters: WebAuthn's Relying Party ID must be a
      domain, so 'localhost' works and '127.0.0.1' does not.)
  2. Paste the code, then approve with Windows Hello / Touch ID / your
     security key.

  Single-use, expires in ${SENTINEL_ENROLLMENT_TTL_MINUTES:-10} minutes.
  Run this again with a SECOND device — that is the whole recovery plan.

EOF
