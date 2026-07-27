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
#   ./scripts/enroll-operator.sh                 # first operator, default label
#   ./scripts/enroll-operator.sh bob "yubikey"   # explicit user + label
set -euo pipefail
cd "$(dirname "$0")/.."

USERNAME="${1:-${SENTINEL_OPERATOR:-$(id -un)}}"
LABEL="${2:-$(hostname) $(date +%Y-%m-%d 2>/dev/null || echo device)}"
PORT="${SENTINEL_ADMIN_PORT:-8400}"
RP_ID="${SENTINEL_RP_ID:-localhost}"

CODE=$(.venv/bin/python - "$USERNAME" "$LABEL" <<'PY'
import sys
from app.auth import mint_enrollment_code
from app.db import SessionLocal

with SessionLocal() as s:
    print(mint_enrollment_code(s, sys.argv[1], sys.argv[2]))
PY
)

cat <<EOF

  Enrollment code for '$USERNAME' ($LABEL):

      $CODE

  1. Open  http://$RP_ID:$PORT/   in a browser ON THIS HOST.
     (The hostname matters: WebAuthn's Relying Party ID must be a
      domain, so 'localhost' works and '127.0.0.1' does not.)
  2. Paste the code, then approve with Windows Hello / Touch ID / your
     security key.

  The code is single-use and expires in ${SENTINEL_ENROLLMENT_TTL_MINUTES:-10} minutes.

EOF
