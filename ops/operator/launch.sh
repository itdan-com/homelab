#!/usr/bin/env bash
# Launch the OPERATOR Claude session (interactive, Phase 4.5) or run
# ONE scheduled tick of it (--tick, Phase 6.2 — Mission Control's
# continuous loop; normally invoked by the operator-tick systemd timer).
# Usage:
#   bash ~/homelab/ops/operator/launch.sh                # interactive
#   bash ~/homelab/ops/operator/launch.sh --tick         # one headless pass
#   bash ~/homelab/ops/operator/launch.sh --tick --force-agent
#       # wake the agent even on a green envelope (testing; still
#       # subject to every guard below)
set -euo pipefail

MODE=interactive
FORCE_AGENT=0
for a in "$@"; do
  case "$a" in
    --tick) MODE=tick ;;
    --force-agent) FORCE_AGENT=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

REPO="$HOME/homelab-operator/repo"
STATE_DIR="$HOME/.config/homelab-operator"
OBS_LOG="$STATE_DIR/observations.log"
TICK_STATE="$STATE_DIR/tick-state.json"
set -a; source "$STATE_DIR/env"; set +a

# Tick guard knobs — overridable in the env file, defaults reviewed here.
: "${TICK_MODEL:=sonnet}"
: "${TICK_MAX_OPEN_PRS:=3}"
: "${TICK_COOLDOWN_MIN:=60}"
: "${TICK_MAX_AGENT_RUNS_PER_DAY:=20}"

# Sync the operator's private clone to main using a fresh 1h App token
# (the remote URL stays credential-free). The hard reset + clean is
# also the tick's fail-safe: an errored previous pass leaves nothing
# half-done in the workspace.
#
# ADR-009 D2: GitHub failing must DEGRADE this script, not abort it —
# the first draft died at the token mint under set -e, before the
# envelope check (which needs no GitHub) ever ran, so a GitHub outage
# silently blinded the watchman. sync_repo classifies instead:
#   GH_STATE=ok            token minted, clone synced
#   GH_STATE=unreachable   network-shaped failure -> calm degrade
#   GH_STATE=auth_refused  401/403 from a reachable GitHub — the
#                          owner-side kill switch (App uninstall) looks
#                          exactly like this, so it stays LOUD
TOKEN=""
GH_STATE=ok
sync_repo() {
  local rc=0
  TOKEN=$("$(dirname "${BASH_SOURCE[0]}")/bin/gh-app-token.sh") || rc=$?
  case "$rc" in
    0) ;;
    8) GH_STATE=auth_refused; return 0 ;;
    *) GH_STATE=unreachable;  return 0 ;;
  esac
  # timeout: a blackholed git transport must not wedge the tick
  # against the unit's 900s ceiling (the third call site is guard 3's
  # `gh pr list`, bounded where it runs below).
  if ! timeout 30 git -C "$REPO" fetch -q \
       "https://x-access-token:${TOKEN}@github.com/${GH_REPO}.git" main; then
    GH_STATE=unreachable; return 0
  fi
  git -C "$REPO" reset -q --hard FETCH_HEAD
  git -C "$REPO" checkout -qB main
  git -C "$REPO" clean -qfd
}

# The operator must NOT inherit the human's personal MCP servers.
# Without this, a session launched here picks up whatever is configured
# in ~/.claude.json — Gmail, Drive, Notion, whatever the owner has
# connected — and the "read-only cluster, PR-only GitHub" isolation this
# script advertises would be a half-truth: the operator could read the
# owner's mail. --strict-mcp-config uses ONLY the config named by
# --mcp-config and ignores every other source; the config named is
# deliberately empty. (Owner spotted this 2026-07-28: "making sure
# claude ITSELF doesnt have any of my personal mcp servers".) Any MCP
# the operator SHOULD have gets added to that file explicitly and
# reviewably — never by inheritance.
EMPTY_MCP="$(mktemp)"; printf '{"mcpServers":{}}' > "$EMPTY_MCP"
trap 'rm -f "$EMPTY_MCP"' EXIT

# Read-only cluster eyes. (Bot GitHub hands — GH_TOKEN — are exported
# per-mode AFTER sync_repo has run and classified GitHub's state.)
export KUBECONFIG="$STATE_DIR/kubeconfig"

if [ "$MODE" = interactive ]; then
  sync_repo
  # ADR-009 D2.5: GitHub down must not lock the owner out of their own
  # operator's eyes — start degraded (read-only look, no PR hands)
  # instead of refusing. The first draft aborted here, which meant the
  # human escape hatch shared GitHub's fate.
  case "$GH_STATE" in
    unreachable)
      echo "!!! GITHUB UNREACHABLE — operator starts DEGRADED: cluster eyes work,"
      echo "!!! PR/issue hands do not. Clone may be one tick stale." ;;
    auth_refused)
      echo "!!! GITHUB REFUSED THE APP (401/403) — if you did not uninstall or"
      echo "!!! revoke it, treat that as the incident. Starting degraded." ;;
  esac
  [ "$GH_STATE" = ok ] || TOKEN=""
  export GH_TOKEN="$TOKEN"
  cd "$REPO/ops/operator"
  echo ">>> OPERATOR session: read-only cluster, PR-only GitHub (itdan-homelab-operator[bot])."
  echo ">>> No inherited MCP servers (--strict-mcp-config)."
  echo ">>> Ask it things like: 'give the ai-gateway a warm spare replica'"
  exec claude --strict-mcp-config --mcp-config "$EMPTY_MCP"
fi

# ----- tick mode ------------------------------------------------------
# Design (phase-06 item 6.2): a deterministic envelope check decides IF
# the agent wakes; script-enforced guards bound how often and how much;
# the agent only ever diagnoses the findings and proposes PRs/issues.
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log() { printf '%s %s\n' "$TS" "$*" >> "$OBS_LOG"; }

# ADR-009 D2.1: the envelope runs FIRST — it needs only the kubeconfig
# and the local doors, so the platform keeps its watchman through any
# GitHub outage. It runs from the PREVIOUS tick's clone (the sync
# happens after), which is at most one tick stale — acceptable for a
# script that changes rarely, and the price of not being blind.
ENV_RC=0
ENV_REPORT="$("$REPO/ops/operator/bin/envelope-check.sh" 2>&1)" || ENV_RC=$?
ENV_LINE="$(printf '%s\n' "$ENV_REPORT" | tail -1)"
ENV_FLAT="$(printf '%s\n' "$ENV_REPORT" | head -n -1 | paste -sd' ' -)"
case "$ENV_LINE" in
  ENVELOPE=green)      ANOMALIES="" ;;
  ENVELOPE=anomaly:*)  ANOMALIES="${ENV_LINE#ENVELOPE=anomaly:}" ;;
  *)                   ANOMALIES="envelope_check_failed"
                       ENV_FLAT="envelope-check crashed rc=$ENV_RC: $(printf '%s' "$ENV_REPORT" | tail -c 300 | tr '\n' ' ')" ;;
esac

# ADR-009 D2.2: NOW the GitHub leg — after the eyes, with the outcome
# classified instead of fatal.
sync_repo
case "$GH_STATE" in
  auth_refused)
    # A reachable GitHub saying 401/403 is what App revocation — the
    # documented owner-side kill switch — looks like. It must page,
    # not blend into outage noise: log its own verdict and FAIL the
    # unit so OnFailure= fires (ADR-009 D2.2 review finding).
    log "verdict=github_auth_refused (App revoked/uninstalled? — if not, that IS the incident) $ENV_FLAT"
    exit 1 ;;
  unreachable)
    # The gate fails closed for the agent (no PR, no issue — correct),
    # the watchman stays alive (envelope above ran), and the fact is a
    # calm logged verdict, mirroring the agent-error path. The push
    # alert for this condition is cluster-side: ArgoCD apps flip
    # sync-Unknown and the ADR-009 D5 rule fires.
    log "verdict=github_unreachable anomalies=${ANOMALIES:-none} $ENV_FLAT"
    exit 0 ;;
esac
export GH_TOKEN="$TOKEN"

if [ -z "$ANOMALIES" ] && [ "$FORCE_AGENT" = 0 ]; then
  log "verdict=green github=ok $ENV_FLAT"
  exit 0
fi
[ -z "$ANOMALIES" ] && ANOMALIES="forced"

# Guard 1+2: per-concern cooldown and the daily agent-run ceiling —
# enforced HERE, before any tokens are spent.
GUARD="$(python3 - "$TICK_STATE" "$ANOMALIES" "$TICK_COOLDOWN_MIN" "$TICK_MAX_AGENT_RUNS_PER_DAY" <<'PY'
import json, sys, time, os, datetime
path, anoms, cool_min, max_daily = sys.argv[1], sys.argv[2].split(","), int(sys.argv[3]), int(sys.argv[4])
today = datetime.date.today().isoformat(); now = time.time()
st = {"day": today, "runs_today": 0, "cooldowns": {}}
if os.path.exists(path):
    try: st = json.load(open(path))
    except Exception: pass
if st.get("day") != today:
    st = {"day": today, "runs_today": 0, "cooldowns": {}}
if st["runs_today"] >= max_daily:
    print("DAILY_CAPPED"); raise SystemExit
print("RUNNABLE=" + ",".join(a for a in anoms if now - st["cooldowns"].get(a, 0) >= cool_min * 60))
PY
)"
if [ "$GUARD" = "DAILY_CAPPED" ]; then
  log "verdict=spend-capped max=$TICK_MAX_AGENT_RUNS_PER_DAY/day anomalies=$ANOMALIES $ENV_FLAT"
  exit 0
fi
RUNNABLE="${GUARD#RUNNABLE=}"
if [ -z "$RUNNABLE" ]; then
  log "verdict=cooldown anomalies=$ANOMALIES $ENV_FLAT"
  exit 0
fi

# Guard 3: open-PR cap. Counted from GitHub truth, not local state.
# Bounded + classified like every other GitHub call (ADR-009 D2.3 —
# the review caught this third call site: a blackhole starting between
# the fetch and here would have wedged the tick on gh's unbounded
# request, then died un-logged under set -e).
OPEN_PRS="$(timeout 15 gh pr list --repo "$GH_REPO" --state open --json number,title,headRefName \
  --jq '[.[] | select(.headRefName | startswith("operator/"))]')" || {
  log "verdict=github_unreachable at=pr-cap anomalies=$RUNNABLE $ENV_FLAT"
  exit 0
}
N_OPEN="$(printf '%s' "$OPEN_PRS" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
SLOTS=$(( TICK_MAX_OPEN_PRS - N_OPEN ))
if [ "$SLOTS" -le 0 ]; then
  log "verdict=pr-capped open=$N_OPEN anomalies=$RUNNABLE $ENV_FLAT"
  exit 0
fi

PROMPT="$(cat "$REPO/ops/operator/tick-prompt.md")

--- CONTEXT (generated $TS) ---

ENVELOPE FINDINGS (investigate only these; 'forced' = manual test run):
$ENV_REPORT

ELIGIBLE FINDINGS after cooldown filter: $RUNNABLE
PR SLOTS AVAILABLE: $SLOTS (of $TICK_MAX_OPEN_PRS max open operator PRs)

OPEN OPERATOR PRS (JSON):
$OPEN_PRS

RECENT OBSERVATIONS (newest last):
$(tail -n 15 "$OBS_LOG" 2>/dev/null || echo '(no log yet)')"

cd "$REPO/ops/operator"
# Which billing identity the pass runs on (6.3): the operator's own
# spend-capped key when ANTHROPIC_API_KEY is in the env file, else
# whatever login `claude` holds — i.e. the owner's.
AUTH="auth=$([ -n "${ANTHROPIC_API_KEY:-}" ] && echo api-key || echo login)"
CLAUDE_RC=0
# </dev/null is load-bearing: claude -p sniffs a non-TTY stdin and an
# already-closed pipe reads as "empty piped input" — the pass then
# exits 0 having produced NOTHING (bug found at the first forced tick;
# the explicit redirect is what claude's own warning recommends).
RESULT_JSON="$(claude -p "$PROMPT" --model "$TICK_MODEL" \
  --strict-mcp-config --mcp-config "$EMPTY_MCP" \
  --allowedTools "Bash,Read,Edit,Write,Grep,Glob" \
  --output-format json </dev/null 2>>"$STATE_DIR/tick-agent.err")" || CLAUDE_RC=$?
# Raw result of the last pass, kept for forensics (overwritten each pass).
printf '%s' "$RESULT_JSON" > "$STATE_DIR/last-result.json"

# Record spend + cooldowns even when the pass failed — a broken agent
# retrying every 5 minutes is exactly what the guards exist to stop.
python3 - "$TICK_STATE" "$RUNNABLE" <<'PY'
import json, sys, time, os, datetime
path, anoms = sys.argv[1], sys.argv[2].split(",")
today = datetime.date.today().isoformat()
st = {"day": today, "runs_today": 0, "cooldowns": {}}
if os.path.exists(path):
    try: st = json.load(open(path))
    except Exception: pass
if st.get("day") != today:
    st = {"day": today, "runs_today": 0, "cooldowns": {}}
st["runs_today"] += 1
now = time.time()
for a in anoms:
    st["cooldowns"][a] = now
json.dump(st, open(path, "w"))
PY

if [ "$CLAUDE_RC" -ne 0 ] || [ -z "$RESULT_JSON" ]; then
  log "verdict=agent-error rc=$CLAUDE_RC empty=$([ -z "$RESULT_JSON" ] && echo yes || echo no) $AUTH anomalies=$RUNNABLE see=tick-agent.err $ENV_FLAT"
  exit 0   # a failed pass is a logged fact; the unit itself succeeded
fi

# Parse from the forensics FILE, not a pipe: `python3 - <<heredoc`
# takes the heredoc as stdin, so piped data silently vanishes — the
# bug behind this session's three PARSE-ERROR ticks.
READOUT="$(python3 - "$STATE_DIR/last-result.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    u = d.get("usage") or {}
    res = d.get("result") or ""
    acts = [l.strip() for l in res.splitlines() if l.strip().startswith("ACTION:")]
    line = (acts[-1] if acts else "ACTION: line missing").replace('"', "'")
    print('action="%s" cost=$%.2f in=%s out=%s turns=%s' % (
        line, d.get("total_cost_usd") or 0,
        u.get("input_tokens", 0), u.get("output_tokens", 0), d.get("num_turns", "?")))
except Exception as e:
    print('action="PARSE-ERROR: %s"' % str(e).replace('"', "'"))
PY
)"
log "verdict=agent $AUTH anomalies=$RUNNABLE $READOUT $ENV_FLAT"
