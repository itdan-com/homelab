#!/usr/bin/env bash
# READ-ONLY probe of the owner's Okta org for XAA / ID-JAG readiness —
# run in the SAME one-time API-token session as okta-register-door.sh,
# then revoke the token. Nothing here creates, changes, or deletes
# anything: every call is a GET.
#
#   ./scripts/okta-xaa-probe.sh /tmp/okta-token [org-url]
#
# What it answers, authoritatively (the admin API, not discovery-doc
# inference):
#   1. the org's FEATURE FLAGS containing agent/XAA/token-exchange —
#      the real switch my outside-the-door metadata probe could only
#      guess at
#   2. the custom authorization servers that exist (can this org mint
#      at a custom AS at all)
#   3. anything AI-agent-shaped in the app/directory surface (the
#      owner reports agents exist — name what the API shows)
#
# The verdict decides 7.8.3's live leg: if the org can be made to mint
# ID-JAGs, the EMA receiver's live test runs against the owner's OWN
# tenant — "okta literally" — instead of the xaa.dev playground.
set -euo pipefail

TOKEN_FILE="${1:?usage: $0 <api-token-file> [org-url]}"
ORG="${2:-${OKTA_ORG:-https://integrator-4949708.okta.com}}"
TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
AUTH="Authorization: SSWS $TOKEN"

get() { curl -fsS -m 20 -H "$AUTH" -H "Accept: application/json" "$ORG$1"; }
probe() { # PATH -> status code only (endpoints that may not exist)
  curl -s -o /dev/null -w '%{http_code}' -m 20 -H "$AUTH" \
    -H "Accept: application/json" "$ORG$1"
}

echo "== 1. feature flags mentioning agent / XAA / token exchange"
get "/api/v1/features?limit=200" | python3 -c "
import sys, json
feats = json.load(sys.stdin)
hits = [f for f in feats
        if any(k in (f.get('name') or '').lower()
               for k in ('agent', 'cross app', 'xaa', 'token exchange',
                         'identity assertion', 'id-jag'))]
if not hits:
    print('   (none match — XAA/token-exchange is not surfaced as a')
    print('    self-serve flag; enablement goes through Okta support/')
    print('    developers@okta.com, as the 2026-06-30 EA change said)')
for f in hits:
    print(f\"   {f.get('status','?'):10s}  {f.get('name')}  (stage: {(f.get('stage') or {}).get('value','?')})\")"

echo
echo "== 2. authorization servers in the org"
get "/api/v1/authorizationServers?limit=50" | python3 -c "
import sys, json
for a in json.load(sys.stdin):
    print(f\"   {a['name']:24s} issuer={a['issuer']} status={a['status']}\")"

echo
echo "== 3. AI-agent-shaped objects"
for path in "/api/v1/agents" "/api/v1/agent-connections" \
            "/api/v1/xaa/connections" "/api/v1/delegations"; do
  code=$(probe "$path?limit=5")
  echo "   GET $path -> $code"
done
get "/api/v1/apps?limit=100" | python3 -c "
import sys, json
apps = json.load(sys.stdin)
ai = [a for a in apps
      if any(k in ((a.get('label') or '') + a.get('name', '')).lower()
             for k in ('agent', 'claude', 'mcp', 'requester'))]
print(f'   {len(apps)} apps total; agent/claude/mcp-shaped:')
for a in ai:
    print(f\"     {a.get('label')}  (name={a.get('name')}, signOn={a.get('signOnMode')})\")
if not ai:
    print('     (none by label — the agent surface may live outside /apps)')"

echo
echo "== verdict guide: an ENABLED agent/XAA flag in section 1 plus a"
echo "   custom AS in section 2 means the org can likely mint ID-JAGs"
echo "   once the XAA objects are configured — the EMA receiver's live"
echo "   test then runs against THIS org. No flag = xaa.dev remains"
echo "   the receiver's live harness, and section 1's output is the"
echo "   evidence to attach when asking Okta for the EA flag."