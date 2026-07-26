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
# =============================================================================
set -euo pipefail

: "${GH_APP_ID:?set GH_APP_ID}"
: "${GH_APP_KEY_FILE:?set GH_APP_KEY_FILE}"
CACHE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/homelab-operator"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

NOW=$(date +%s)
HEADER=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
PAYLOAD=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((NOW-60))" "$((NOW+540))" "$GH_APP_ID" | b64url)
SIG=$(printf '%s.%s' "$HEADER" "$PAYLOAD" | openssl dgst -sha256 -sign "$GH_APP_KEY_FILE" -binary | b64url)
JWT="$HEADER.$PAYLOAD.$SIG"

# Discover (and cache) the installation id.
INST_FILE="$CACHE_DIR/installation_id"
if [ ! -s "$INST_FILE" ]; then
  curl -fsS -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
    https://api.github.com/app/installations \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])' > "$INST_FILE"
fi
INSTALLATION_ID=$(cat "$INST_FILE")

curl -fsS -X POST -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/app/installations/$INSTALLATION_ID/access_tokens" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'
