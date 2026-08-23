#!/usr/bin/env bash
# Register the door as an OIDC Web App in the owner's Okta org — so
# the owner's part of the 7.8.1 live matrix shrinks to two acts that
# genuinely cannot be delegated: minting an API token (their admin
# identity) and signing in (the test itself).
#
#   1. Okta admin console -> Security -> API -> Tokens -> Create token
#   2. Save it to a file (NOT the shell history, NOT this repo):
#        umask 077; nano /tmp/okta-token   (paste, save)
#   3. ./scripts/okta-register-door.sh /tmp/okta-token [org-url]
#   4. REVOKE the token in the Okta console when done — it is an
#      admin credential and this script needs it exactly once.
#
# What it creates: one OIDC Web application ("Sentinel door (dev
# matrix)"), authorization-code grant only, redirect URI pointing at
# the no-root dev stack, assigned to the Everyone group so the owner's
# own account can sign in. Prints the client_id/secret ready to paste
# into okta-live-matrix.sh. Idempotent: an existing app with the same
# label is reused, not duplicated.
set -euo pipefail

TOKEN_FILE="${1:?usage: $0 <api-token-file> [org-url]}"
ORG="${2:-${OKTA_ORG:-https://integrator-4949708.okta.com}}"
DOOR_PORT="${SENTINEL_DEV_DOOR_PORT:-8412}"
REDIRECT="https://localhost:$DOOR_PORT/callback"
LABEL="Sentinel door (dev matrix)"
TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
AUTH="Authorization: SSWS $TOKEN"

api() { # METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  curl -fsS -m 20 -X "$method" -H "$AUTH" -H "Accept: application/json" \
    ${body:+-H "Content-Type: application/json" -d "$body"} \
    "$ORG$path"
}

echo "== looking for an existing '$LABEL' app"
EXISTING=$(api GET "/api/v1/apps?q=Sentinel&limit=20" | python3 -c "
import sys, json
apps = json.load(sys.stdin)
for a in apps:
    if a.get('label') == '$LABEL':
        print(a['id']); break")

if [[ -n "$EXISTING" ]]; then
  APP_ID="$EXISTING"
  echo "== reusing app $APP_ID"
else
  echo "== creating the OIDC web app"
  APP_ID=$(api POST "/api/v1/apps" "$(python3 - <<PY
import json
print(json.dumps({
  "name": "oidc_client", "label": "$LABEL",
  "signOnMode": "OPENID_CONNECT",
  "credentials": {"oauthClient": {"token_endpoint_auth_method": "client_secret_basic"}},
  "settings": {"oauthClient": {
      "redirect_uris": ["$REDIRECT"],
      "response_types": ["code"],
      "grant_types": ["authorization_code"],
      "application_type": "web"}},
}))
PY
)" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
  echo "== app created: $APP_ID"
fi

echo "== assigning the Everyone group (so your own account can sign in)"
EVERYONE=$(api GET "/api/v1/groups?q=Everyone&limit=5" | python3 -c "
import sys, json
for g in json.load(sys.stdin):
    if g['profile']['name'] == 'Everyone':
        print(g['id']); break")
api PUT "/api/v1/apps/$APP_ID/groups/$EVERYONE" "{}" >/dev/null || true

CREDS=$(api GET "/api/v1/apps/$APP_ID" | python3 -c "
import sys, json
a = json.load(sys.stdin)
oc = a['credentials']['oauthClient']
print(oc['client_id'], oc.get('client_secret', '<hidden — regenerate in console if needed>'))")

echo
echo "== DONE. Now run the matrix:"
echo
echo "   SENTINEL_OIDC_CLIENT_ID=${CREDS%% *} \\"
echo "   SENTINEL_OIDC_CLIENT_SECRET=${CREDS#* } \\"
echo "   ./scripts/okta-live-matrix.sh up $ORG"
echo
echo "== and REVOKE the API token in the Okta console — its job is done."
