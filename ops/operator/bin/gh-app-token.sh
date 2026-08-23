#!/usr/bin/env bash
# =============================================================================
# Mint a short-lived (1h) GitHub App installation token for the operator.
#
# The operator holds NO long-lived GitHub credential: it signs a 9-minute
# JWT with the App's private key, exchanges it for a 1-hour installation
# token scoped to exactly the repos the App is installed on, and uses that.
# Revocation = uninstall the App in the GitHub UI (owner-side kill switch).
#
# Env (see ~/.config/homelab-operator/env):
#   GH_APP_ID         numeric App ID
#   GH_APP_KEY_FILE   path to the App's private key (.pem)
#
# Usage:  GH_TOKEN=$("$(dirname "$0")/gh-app-token.sh")   # then use gh / git
#
# Exit codes (ADR-009 D2 — the caller must tell an outage from a cut):
#   0  token on stdout
#   7  GitHub UNREACHABLE (network-shaped: timeout, refused, DNS, 5xx)
#      -> the caller degrades calmly and retries next tick
#   8  GitHub REFUSED us (401/403 from a reachable API) — the App may
#      be revoked/uninstalled, which is the documented owner-side kill
#      switch -> the caller must stay LOUD, never blend this into
#      outage noise
# GH_API_BASE overrides the API host (rehearsal against stubs).
# =============================================================================
set -euo pipefail

: "${GH_APP_ID:?set GH_APP_ID}"
: "${GH_APP_KEY_FILE:?set GH_APP_KEY_FILE}"
CACHE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/homelab-operator"
API="${GH_API_BASE:-https://api.github.com}"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# Bounded + classified fetch: 10s timeout so a blackholed GitHub can
# never wedge the tick against the unit's 900s ceiling (ADR-009 D2.3).
BODY_FILE="$(mktemp)"; trap 'rm -f "$BODY_FILE"' EXIT
_api() { # METHOD URL -> body in $BODY_FILE; returns 0/7/8
  local method="$1" url="$2" code
  code=$(curl -sS -m 10 --retry 2 -X "$method" \
    -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
    -o "$BODY_FILE" -w '%{http_code}' "$url" 2>/dev/null) || code=000
  case "$code" in
    2*) return 0 ;;
    401|403) echo "gh-app-token: GitHub REFUSED ($code) — App revoked/uninstalled?" >&2
             return 8 ;;
    *)  echo "gh-app-token: GitHub unreachable (http=$code)" >&2
        return 7 ;;
  esac
}

NOW=$(date +%s)
HEADER=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
PAYLOAD=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((NOW-60))" "$((NOW+540))" "$GH_APP_ID" | b64url)
SIG=$(printf '%s.%s' "$HEADER" "$PAYLOAD" | openssl dgst -sha256 -sign "$GH_APP_KEY_FILE" -binary | b64url)
JWT="$HEADER.$PAYLOAD.$SIG"

# Discover (and cache) the installation id.
INST_FILE="$CACHE_DIR/installation_id"
if [ ! -s "$INST_FILE" ]; then
  _api GET "$API/app/installations"
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[0]["id"])' "$BODY_FILE" > "$INST_FILE"
fi
INSTALLATION_ID=$(cat "$INST_FILE")

_api POST "$API/app/installations/$INSTALLATION_ID/access_tokens"
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$BODY_FILE"
