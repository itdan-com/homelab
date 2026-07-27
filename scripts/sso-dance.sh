#!/usr/bin/env bash
# =============================================================================
# sso-dance.sh — verify the LIVE SSO flows, not the objects.
#
# Born from the Phase 5 B5 lesson: every OIDC object can be green while
# every real login bounces (the empty-grant_types bug). So this script
# performs the actual dance, headless, end to end:
#
#   1. Log into Authentik as akadmin via the flow executor API
#      (identification -> password -> redirect), like a browser would.
#   2. For each app (OpenWebUI, Grafana, ArgoCD): hit the app's own
#      OIDC initiation route, ride the full redirect chain through
#      authorize -> callback, then ask the app "who am I / what may I
#      do" and assert the role that group membership promises.
#   3. Impersonate a non-admin (default: bob) through Authentik's
#      admin API and repeat Grafana + ArgoCD, asserting the LESSER
#      roles (Viewer / read-only) — proving role mapping is real, not
#      just "login works".
#
# TLS is verified against the lab CA pulled live from the cluster —
# so an accidental CA rotation fails loudly here too.
#
# Env overrides: DOMAIN (lab.local)  PORT (8443)  RESOLVE_IP (127.0.0.1)
#                IMPERSONATE_USER (bob)
# Exit: 0 only if every assertion passed.
# =============================================================================
set -uo pipefail

DOMAIN="${DOMAIN:-lab.local}"
PORT="${PORT:-8443}"
RESOLVE_IP="${RESOLVE_IP:-127.0.0.1}"
IMPERSONATE_USER="${IMPERSONATE_USER:-bob}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AK="https://authentik.${DOMAIN}:${PORT}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

# --- plumbing -----------------------------------------------------------------
RESOLVES=()
for h in authentik openwebui grafana argocd; do
  RESOLVES+=(--resolve "${h}.${DOMAIN}:${PORT}:${RESOLVE_IP}")
done
CA="$WORK/ca.crt"
kubectl get secret lab-local-ca -n cert-manager -o jsonpath='{.data.ca\.crt}' | base64 -d > "$CA" \
  || { echo "cannot pull lab CA from cluster"; exit 1; }

curlx() { # curlx <jar> <args...>
  local jar="$1"; shift
  curl -s --cacert "$CA" "${RESOLVES[@]}" -c "$jar" -b "$jar" --max-time 30 "$@"
}

pyget() { # pyget '<python expr over d>'  (JSON on stdin)
  python3 -c "import sys,json; d=json.load(sys.stdin); print($1)" 2>/dev/null
}

csrf_of() { awk '$0 ~ /authentik_csrf/ {v=$NF} END {print v}' "$1"; }

# Copy only the Authentik session cookies into a fresh jar, so app
# dances for a second identity never reuse the first identity's app
# sessions.
seed_jar() { # seed_jar <src> <dst>
  grep -E "(^|_)authentik\.${DOMAIN}" "$1" > "$2" 2>/dev/null || true
}

# --- authentik login via flow executor ---------------------------------------
ak_login() { # ak_login <jar> <user> <pass>
  local jar="$1" user="$2" pass="$3" exec_url="$AK/api/v3/flows/executor/default-authentication-flow/?query="
  curlx "$jar" -o /dev/null "$AK/if/flow/default-authentication-flow/"
  local i comp body
  for i in 1 2 3 4 5 6; do
    # -L: the executor answers stage POSTs *and* flow completion with
    # redirects (PRG); the session only turns authenticated once the
    # completion redirect is actually followed.
    body="$(curlx "$jar" -L "$exec_url")"
    comp="$(echo "$body" | pyget 'd.get("component","")')"
    case "$comp" in
      ak-stage-identification)
        local with_pw payload
        with_pw="$(echo "$body" | pyget 'd.get("password_fields",False)')"
        if [ "$with_pw" = "True" ]; then
          payload="$(python3 -c "import json,sys;print(json.dumps({'uid_field':sys.argv[1],'password':sys.argv[2]}))" "$user" "$pass")"
        else
          payload="$(python3 -c "import json,sys;print(json.dumps({'uid_field':sys.argv[1]}))" "$user")"
        fi
        curlx "$jar" -o /dev/null -X POST -H 'Content-Type: application/json' \
          -H "X-authentik-CSRF: $(csrf_of "$jar")" -H "Referer: $AK/" -d "$payload" "$exec_url" ;;
      ak-stage-password)
        curlx "$jar" -o /dev/null -X POST -H 'Content-Type: application/json' \
          -H "X-authentik-CSRF: $(csrf_of "$jar")" -H "Referer: $AK/" \
          -d "$(python3 -c "import json,sys;print(json.dumps({'password':sys.argv[1]}))" "$pass")" "$exec_url" ;;
      xak-flow-redirect) break ;;
      "") break ;;
    esac
  done
  local me; me="$(curlx "$jar" "$AK/api/v3/core/users/me/" | pyget 'd["user"]["username"]')"
  [ "$me" = "$user" ]
}

# --- per-app dances -----------------------------------------------------------
dance_openwebui() { # <jar> <label> <expected role>
  local jar="$1" label="$2" want="$3" base="https://openwebui.${DOMAIN}:${PORT}"
  curlx "$jar" -o /dev/null -L "$base/oauth/oidc/login"
  local tok role
  tok="$(awk '$0 ~ /\ttoken\t/ {v=$NF} END {print v}' "$jar")"
  [ -z "$tok" ] && { bad "openwebui $label: no session token after OIDC dance"; return; }
  role="$(curlx "$jar" -H "Authorization: Bearer $tok" "$base/api/v1/auths/" | pyget 'd.get("role","")')"
  [ "$role" = "$want" ] && ok "openwebui $label: OIDC dance + role=$role" \
                        || bad "openwebui $label: role='$role' want='$want'"
}

dance_grafana() { # <jar> <label> <expected org role>
  local jar="$1" label="$2" want="$3" base="https://grafana.${DOMAIN}:${PORT}"
  curlx "$jar" -o /dev/null -L "$base/login/generic_oauth"
  local login role
  login="$(curlx "$jar" "$base/api/user" | pyget 'd.get("login","")')"
  [ -z "$login" ] && { bad "grafana $label: not logged in after OIDC dance"; return; }
  role="$(curlx "$jar" "$base/api/user/orgs" | pyget 'd[0]["role"]')"
  [ "$role" = "$want" ] && ok "grafana $label: OIDC dance as '$login' + role=$role" \
                        || bad "grafana $label: role='$role' want='$want'"
}

dance_argocd() { # <jar> <label> <expected can-sync yes|no>
  local jar="$1" label="$2" want="$3" base="https://argocd.${DOMAIN}:${PORT}"
  curlx "$jar" -o /dev/null -L "$base/auth/login"
  local who cani
  who="$(curlx "$jar" "$base/api/v1/session/userinfo" | pyget 'd.get("username","")')"
  [ -z "$who" ] && { bad "argocd $label: not logged in after OIDC dance"; return; }
  # subresource is project/name — '*/*' means "any app in any project"
  cani="$(curlx "$jar" "$base/api/v1/account/can-i/applications/sync/*/*" | pyget 'd.get("value","")')"
  [ "$cani" = "$want" ] && ok "argocd $label: OIDC dance as '$who' + can-sync=$cani" \
                        || bad "argocd $label: can-sync='$cani' want='$want'"
}

# --- run ----------------------------------------------------------------------
echo "== SSO dance against *.${DOMAIN}:${PORT} (resolve ${RESOLVE_IP}) =="

AK_PW="$(sops -d "$REPO_ROOT/catalog/authentik/secrets.enc.yaml" | python3 -c '
import sys, yaml
def walk(d):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == "bootstrap_password": print(v); return True
            if walk(v): return True
    return False
walk(yaml.safe_load(sys.stdin))')"
[ -z "$AK_PW" ] && { echo "cannot read bootstrap_password via sops"; exit 1; }

JAR="$WORK/akadmin.jar"
if ak_login "$JAR" akadmin "$AK_PW"; then
  ok "authentik: akadmin flow-executor login"
  dance_openwebui "$JAR" akadmin admin
  dance_grafana   "$JAR" akadmin Admin
  dance_argocd    "$JAR" akadmin yes
else
  bad "authentik: akadmin flow-executor login"
fi

# --- lesser-role checks via impersonation ------------------------------------
UID_JSON="$(curlx "$JAR" "$AK/api/v3/core/users/?username=${IMPERSONATE_USER}")"
BOB_PK="$(echo "$UID_JSON" | pyget 'd["results"][0]["pk"]')"
if [ -n "$BOB_PK" ]; then
  HTTP="$(curlx "$JAR" -o /dev/null -w '%{http_code}' -X POST \
    -H "X-authentik-CSRF: $(csrf_of "$JAR")" -H "Referer: $AK/" \
    -H 'Content-Type: application/json' \
    -d '{"reason": "sso-dance.sh role verification"}' \
    "$AK/api/v3/core/users/${BOB_PK}/impersonate/")"
  if [ "$HTTP" = "201" ] || [ "$HTTP" = "204" ] || [ "$HTTP" = "200" ]; then
    ok "authentik: impersonating ${IMPERSONATE_USER} (pk=$BOB_PK)"
    JAR2="$WORK/imp.jar"; seed_jar "$JAR" "$JAR2"
    dance_grafana "$JAR2" "$IMPERSONATE_USER" Viewer
    JAR3="$WORK/imp2.jar"; seed_jar "$JAR" "$JAR3"
    dance_argocd  "$JAR3" "$IMPERSONATE_USER" no
    curlx "$JAR" -o /dev/null -X POST -H "X-authentik-CSRF: $(csrf_of "$JAR")" \
      -H "Referer: $AK/" -H 'Content-Type: application/json' -d '{}' \
      "$AK/api/v3/core/users/impersonate_end/"
  else
    bad "authentik: impersonate ${IMPERSONATE_USER} returned HTTP $HTTP"
  fi
else
  bad "authentik: user '${IMPERSONATE_USER}' not found for impersonation"
fi

echo "== summary: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
