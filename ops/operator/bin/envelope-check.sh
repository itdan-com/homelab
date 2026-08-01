#!/usr/bin/env bash
# Deterministic platform envelope check — the tick's watchman.
# NO model, NO GitHub token, read-only cluster credentials only.
# Prints one "name=verdict(detail)" line per check, then a final
# summary line the tick parses:
#   ENVELOPE=green
#   ENVELOPE=anomaly:<name>[,<name>...]
# Exit 0 on green, 1 on any anomaly. The model is the diagnostician,
# not the watchman: this script decides IF the agent wakes, never what
# it does.
set -uo pipefail

KUBECONFIG="${KUBECONFIG:-$HOME/.config/homelab-operator/kubeconfig}"
export KUBECONFIG

# Platform constants (in-repo values, not host-specifics; override via env)
KEDA_CEILING="${KEDA_CEILING:-3}"                # catalog/ai-gateway values
TOKENS_WARN="${TOKENS_WARN:-72}"                 # 0.8 * ceiling * 30/replica
GATEWAY_NS="${GATEWAY_NS:-envoy-gateway-system}"
DOORS_ORIGIN="${DOORS_ORIGIN:-https://localhost:8443}"
PROM_BASE="/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090/proxy/api/v1/query"

ANOMALIES=()
note() { printf '%s\n' "$1"; }
flag() { ANOMALIES+=("$1"); printf '%s\n' "$2"; }

# --- 1. API reachability (everything else depends on it) -------------
if ! kubectl get --raw /readyz --request-timeout=10s >/dev/null 2>&1; then
  flag api_unreachable "api=UNREACHABLE(readyz failed or timed out)"
else
  note "api=ok"

  # --- 2. Nodes ------------------------------------------------------
  # "Fewer than expected" is an anomaly, not an ok — an RBAC denial or
  # a vanished node both surface here instead of hiding in /dev/null
  # (bug caught at first live run: view RBAC couldn't list nodes and
  # 0/4 printed as ok).
  NODE_COUNT="${NODE_COUNT:-4}"
  NODES_RAW=$(kubectl get nodes --no-headers 2>/dev/null || true)
  TOTAL=$(printf '%s' "$NODES_RAW" | grep -c . || true)
  NOT_READY=$(printf '%s\n' "$NODES_RAW" | awk 'NF && $2!="Ready"{print $1}' | paste -sd, -)
  if [ "$TOTAL" -lt "$NODE_COUNT" ]; then
    flag nodes_missing "nodes=MISSING(sees ${TOTAL} of ${NODE_COUNT} — node gone, or RBAC denies the list)"
  elif [ -n "$NOT_READY" ]; then flag nodes_not_ready "nodes=NOT_READY(${NOT_READY})"
  else note "nodes=ok(${TOTAL}/${NODE_COUNT} Ready)"; fi

  # --- 3. Pods (anything neither Running nor Completed) --------------
  BAD_PODS=$(kubectl get pods -A --no-headers 2>/dev/null \
    | awk '$4!="Running" && $4!="Completed" && $4!="Succeeded" {print $1"/"$2":"$4}' | head -5 | paste -sd, -)
  if [ -n "$BAD_PODS" ]; then flag pods_unhealthy "pods=UNHEALTHY(${BAD_PODS})"
  else note "pods=ok"; fi

  # --- 4. ArgoCD app convergence -------------------------------------
  BAD_APPS=$(kubectl get applications -n argocd -o json 2>/dev/null | python3 -c "
import json,sys
try: apps=json.load(sys.stdin)['items']
except Exception: print('QUERY_FAILED'); raise SystemExit
bad=[a['metadata']['name']+':'+a['status'].get('sync',{}).get('status','?')+'/'+a['status'].get('health',{}).get('status','?')
     for a in apps
     if a['status'].get('sync',{}).get('status')!='Synced' or a['status'].get('health',{}).get('status')!='Healthy']
print(','.join(bad[:5]))")
  if [ -n "$BAD_APPS" ]; then flag argocd_diverged "argocd=DIVERGED(${BAD_APPS})"
  else note "argocd=ok"; fi

  # --- 5. KEDA ceiling (gateway data plane at max replicas) ----------
  GW_READY=$(kubectl get deploy -n "$GATEWAY_NS" --no-headers 2>/dev/null | awk '/^envoy-/{print $2; exit}')
  GW_NOW="${GW_READY%%/*}"
  if [ -n "${GW_NOW:-}" ] && [ "$GW_NOW" -ge "$KEDA_CEILING" ] 2>/dev/null; then
    flag keda_at_ceiling "keda=AT_CEILING(${GW_NOW}/${KEDA_CEILING})"
  else note "keda=ok(${GW_READY:-no-data-plane} of ceiling ${KEDA_CEILING})"; fi

  # --- 6. Token rate vs capacity (the Phase-3 scaling signal) --------
  Q='sum(rate(gen_ai_client_token_usage_sum{gen_ai_token_type="output"}[1m]))'
  RATE=$(kubectl get --raw "${PROM_BASE}?query=$(python3 -c "from urllib.parse import quote;import sys;print(quote(sys.argv[1]))" "$Q")" 2>/dev/null \
    | python3 -c "
import json,sys
try: r=json.load(sys.stdin)['data']['result']
except Exception: print('nan'); raise SystemExit
print(float(r[0]['value'][1]) if r else 0.0)")
  if [ "$RATE" = "nan" ]; then
    flag prometheus_unreadable "tokens=PROM_UNREADABLE(proxy query failed)"
  elif python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) > float(sys.argv[2]) else 1)" "$RATE" "$TOKENS_WARN"; then
    flag high_token_rate "tokens=HIGH(${RATE}/s > warn ${TOKENS_WARN}/s)"
  else note "tokens=ok(${RATE}/s)"; fi
fi

# --- 7. Doors (host-side curl; works even if the API is down) --------
declare -A WANT=( [openwebui]=200 [authentik]=302 [grafana]=302 [argocd]=200 )
BAD_DOORS=""
for h in openwebui authentik grafana argocd; do
  CODE=$(curl -sk -o /dev/null -m 10 -w '%{http_code}' -H "Host: ${h}.lab.local" "${DOORS_ORIGIN}/" 2>/dev/null)
  [ "$CODE" != "${WANT[$h]}" ] && BAD_DOORS="${BAD_DOORS}${h}:${CODE},"
done
if [ -n "$BAD_DOORS" ]; then flag doors_down "doors=DOWN(${BAD_DOORS%,})"
else note "doors=ok(200/302/302/200)"; fi

# --- Summary ---------------------------------------------------------
if [ "${#ANOMALIES[@]}" -eq 0 ]; then
  echo "ENVELOPE=green"; exit 0
else
  echo "ENVELOPE=anomaly:$(IFS=,; echo "${ANOMALIES[*]}")"; exit 1
fi
