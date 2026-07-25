#!/usr/bin/env bash
# =============================================================================
# AI-gateway autoscaling demo (Phase 3 milestone asset).
#
# Drives a bursty AI load (4 users, long generations, 4 minutes) through the
# gateway and samples the scale event: token rate rises -> KEDA grows the
# Envoy data plane toward its ceiling -> load stops -> ~5 min later (HPA
# scale-down stabilization) the fleet collapses back to min.
#
# Watch it in Grafana: dashboard "AI Gateway — Token-Rate Autoscaling"
# (provisioned by catalog/monitoring). Screenshot THAT for the README.
#
# Prereqs:
#   - catalog/ai-gateway installed with autoscaling.mode=keda
#   - catalog/monitoring + catalog/keda installed
#   - a `k6-loadtest` consumer in catalog/ai-gateway/secrets.enc.yaml and a
#     matching Secret:  kubectl create secret generic k6-gateway-key \
#       -n <gateway-ns> --from-literal=API_KEY=<that key>
#   - GPU free: close games/GPU-heavy apps first (see phase-03 notes).
#
# Overridable env: GW_NS, CTRL_NS, GW_NAME, EXEC_POD_DEPLOY, PROM_URL.
# =============================================================================
set -euo pipefail

GW_NS="${GW_NS:-chat}"
CTRL_NS="${CTRL_NS:-envoy-gateway-system}"
GW_NAME="${GW_NAME:-ai-gateway}"
# Any deployment in GW_NS whose image has python3 (used to query Prometheus
# from inside the cluster).
EXEC_POD_DEPLOY="${EXEC_POD_DEPLOY:-openwebui}"
PROM_URL="${PROM_URL:-http://monitoring-kube-prometheus-prometheus.monitoring:9090}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/scale-demo" && pwd)"

DEPLOY=$(kubectl get deploy -n "$CTRL_NS" \
  -l "gateway.envoyproxy.io/owning-gateway-name=$GW_NAME,gateway.envoyproxy.io/owning-gateway-namespace=$GW_NS" \
  -o jsonpath='{.items[0].metadata.name}')
[ -n "$DEPLOY" ] || { echo "ERROR: no Envoy data-plane deployment found for gateway $GW_NS/$GW_NAME"; exit 1; }

kubectl get secret k6-gateway-key -n "$GW_NS" >/dev/null 2>&1 || {
  echo "ERROR: secret k6-gateway-key missing in $GW_NS."
  echo "Mint a 'k6-loadtest' consumer (sops catalog/ai-gateway/secrets.enc.yaml,"
  echo "helm upgrade), then: kubectl create secret generic k6-gateway-key -n $GW_NS --from-literal=API_KEY=<key>"
  exit 1
}

echo ">>> Reminder: GPU must be free (no games) or throughput collapses and the demo stalls."
echo ">>> Data plane: $CTRL_NS/$DEPLOY | baseline replicas: $(kubectl get deploy -n "$CTRL_NS" "$DEPLOY" -o jsonpath='{.status.readyReplicas}')"
echo ">>> Open Grafana dashboard: 'AI Gateway — Token-Rate Autoscaling' (uid ai-gateway-scaling)"

kubectl create configmap k6-burst-script -n "$GW_NS" \
  --from-file=burst.js="$SCRIPT_DIR/burst.js" --dry-run=client -o yaml | kubectl apply -f -
sed "s/namespace: chat/namespace: $GW_NS/" "$SCRIPT_DIR/k6-job.yaml" | kubectl apply -f -
echo ">>> Load running (4 VUs x 4m). Sampling every 20s:"

RATE_PY='import urllib.request,urllib.parse,json,sys;u=sys.argv[1]+"/api/v1/query?query="+urllib.parse.quote("sum(rate(gen_ai_client_token_usage_sum{gen_ai_token_type=\"output\"}[1m]))");r=json.loads(urllib.request.urlopen(u,timeout=8).read())["data"]["result"];print(round(float(r[0]["value"][1]),1) if r else 0)'

for i in $(seq 1 16); do
  TS=$(date +%H:%M:%S)
  DES=$(kubectl get deploy -n "$CTRL_NS" "$DEPLOY" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo '?')
  RDY=$(kubectl get deploy -n "$CTRL_NS" "$DEPLOY" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo '?')
  RATE=$(kubectl exec -n "$GW_NS" "deploy/$EXEC_POD_DEPLOY" -- python3 -c "$RATE_PY" "$PROM_URL" 2>/dev/null || echo '?')
  echo "  $TS  desired=$DES ready=$RDY output-tok/s=$RATE"
  sleep 20
done

kubectl wait --for=condition=complete "job/k6-burst" -n "$GW_NS" --timeout=120s >/dev/null 2>&1 || true
echo ">>> k6 summary:"
kubectl logs -n "$GW_NS" job/k6-burst --tail 40 2>/dev/null | grep -E "checks_succeeded|http_req_duration|http_reqs" || true

kubectl delete job k6-burst -n "$GW_NS" --ignore-not-found >/dev/null
kubectl delete configmap k6-burst-script -n "$GW_NS" --ignore-not-found >/dev/null
echo ">>> Rig torn down. Scale-DOWN lands ~5 min after the rate zeroes (HPA"
echo ">>> stabilization) — keep the dashboard open to capture the full arc."
