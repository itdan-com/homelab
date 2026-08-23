#!/usr/bin/env bash
# 7.8.1's exit criterion, on the wire: the door against a REAL Okta
# org (ADR-008 D2's live test matrix). Runs entirely on the no-root
# dev stack — nothing here touches the installed units, and the only
# human act in the whole matrix is the human authenticating, because
# that IS the test.
#
#   SENTINEL_OIDC_CLIENT_ID=<okta app client id> \
#   SENTINEL_OIDC_CLIENT_SECRET=<its secret> \
#   ./scripts/okta-live-matrix.sh up [org-url]
#       -> brings up the dev door against the org, runs the headless
#          half (discovery, JWKS — through the door's real code
#          path), prints ONE sign-in URL for the human
#
#   ./scripts/okta-live-matrix.sh verify
#       -> after the human signed in: proves the aftermath from the
#          dev DB (principal created, (issuer, sub) pinned, audited)
#
#   ./scripts/okta-live-matrix.sh down
#
# The sign-in URL is /link/github ON PURPOSE: it drives the door's
# full browser federation leg (authorize -> Okta -> callback -> TOFU
# pin -> session cookie) with no MCP client and no PKCE tooling, and
# ends on a "Cannot link github" page — which IS the success signal
# here (the linking refusal comes from having no github upstream in
# the dev rig; the sign-in it required is the thing under test).
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN="${SENTINEL_DEV_DIR:-${TMPDIR:-/tmp}/sentinel-dev}"
ORG="${2:-${OKTA_ORG:-https://integrator-4949708.okta.com}}"
DOOR_PORT="${SENTINEL_DEV_DOOR_PORT:-8412}"
export MATRIX_DIR="$DIR" MATRIX_RUN="$RUN" MATRIX_ORG="$ORG"

up() {
  # (no apostrophe in the :? message — bash parses quotes INSIDE
  # parameter-expansion words even under double quotes)
  : "${SENTINEL_OIDC_CLIENT_ID:?set SENTINEL_OIDC_CLIENT_ID to the Okta app client id}"
  echo "== 7.8.1 live matrix against $ORG"
  # The org AS issuer; public TLS; no split-horizon rewrite. These
  # exports override dev-stack's Authentik defaults for this run.
  export SENTINEL_OIDC_ISSUER="$ORG"
  export SENTINEL_OIDC_HTTP_BASE=""
  export SENTINEL_OIDC_CA_BUNDLE=""
  export SENTINEL_OIDC_CLIENT_AUTH="${SENTINEL_OIDC_CLIENT_AUTH:-basic}"

  "$DIR/scripts/dev-stack.sh" up

  echo
  echo "== headless half: the door's own IdP machinery against the org"
  "$DIR/.venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["MATRIX_DIR"])
os.environ.setdefault("SENTINEL_DB",
                      os.path.join(os.environ["MATRIX_RUN"], "sentinel-dev.db"))
from app import door
org = os.environ["MATRIX_ORG"]
cfg = door.oidc_config()          # discovery through door code, live
assert cfg["issuer"] == org, f"issuer mismatch: {cfg['issuer']}"
key = door._idp_key(None)         # JWKS fetch + first-key parse, live
print("  discovery: issuer/authorize/token/jwks all resolved")
print("  jwks: fetched and parsed an RS256 key:", type(key).__name__)
print("  HEADLESS HALF: PASS")
PY
  echo
  echo "== YOUR ONE ACT: open this in a browser on this machine and"
  echo "   sign in with your Okta account:"
  echo
  echo "     https://localhost:$DOOR_PORT/link/github"
  echo
  echo "   (accept the self-signed cert warning — dev rig; a"
  echo "   'Cannot link github' page at the end means SUCCESS)"
  echo "   Then run:  $0 verify"
}

verify() {
  echo "== the aftermath, from the dev DB"
  "$DIR/.venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["MATRIX_DIR"])
os.environ.setdefault("SENTINEL_DB",
                      os.path.join(os.environ["MATRIX_RUN"], "sentinel-dev.db"))
from sqlalchemy import select
from app.db import SessionLocal
from app.models import AuditEvent, AuditEventType, Principal
org = os.environ["MATRIX_ORG"].rstrip("/")
with SessionLocal() as s:
    ps = s.scalars(select(Principal)).all()
    okta = [p for p in ps if (p.idp_iss or "").rstrip("/") == org]
    if not okta:
        print("  NO principal pinned to", org, "- sign-in has not happened yet")
        sys.exit(1)
    ok = False
    for p in okta:
        print("  principal:", p.email)
        print("    (issuer, sub) pin:", (p.idp_iss, p.idp_sub))
        print("    first_seen:", p.first_seen_at, " disabled:", p.disabled_at)
        rows = s.scalars(select(AuditEvent)
                         .where(AuditEvent.principal == p.email)
                         .order_by(AuditEvent.id)).all()
        kinds = [r.event_type.value + ":" +
                 str((r.details or {}).get("kind")
                     or (r.details or {}).get("surface", ""))
                 for r in rows]
        print("    audit trail:", kinds)
        ok = (p.idp_sub is not None
              and any(r.event_type == AuditEventType.AUTH_SUCCESS
                      for r in rows))
    print()
    print("  LIVE MATRIX: PASS — the door federated a real Okta sign-in"
          if ok else "  LIVE MATRIX: INCOMPLETE — pin or audit missing")
    sys.exit(0 if ok else 1)
PY
}

case "${1:-up}" in
  up) up ;;
  verify) verify ;;
  down) "$DIR/scripts/dev-stack.sh" down ;;
  *) echo "usage: $0 [up [org-url]|verify|down]" >&2; exit 2 ;;
esac
