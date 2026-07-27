#!/usr/bin/env bash
# =============================================================================
# netpol-smoke.sh — prove NetworkPolicy is ENFORCED, not just accepted.
#
# Flannel (the k3s default CNI) accepts NetworkPolicy objects and then
# ignores them — security that looks configured but does nothing. This
# is the live proof that the Cilium swap (Phase 5.5 entry criterion)
# actually changed that:
#
#   1. Baseline: client pod can reach a web pod            -> must PASS
#   2. Apply default-deny ingress to the namespace         -> curl must FAIL
#   3. Apply a scoped allow (only app=client, only :80)    -> curl must PASS
#
# Step 2 succeeding-to-connect is exactly what Flannel would do.
# Cleans up after itself. Exit 0 only if all three behave.
# =============================================================================
set -uo pipefail

NS="netpol-smoke"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
probe() { kubectl exec -n "$NS" client -- curl -s -o /dev/null -m 4 -w '%{http_code}' "http://web.${NS}/" 2>/dev/null; }

kubectl delete ns "$NS" --ignore-not-found --wait=true >/dev/null 2>&1
kubectl create ns "$NS" >/dev/null

kubectl run web -n "$NS" --image=nginx:1.27-alpine --labels=app=web --port=80 >/dev/null
kubectl expose pod web -n "$NS" --port=80 >/dev/null
kubectl run client -n "$NS" --image=curlimages/curl:8.9.1 --labels=app=client \
  --command -- sleep 900 >/dev/null
kubectl wait -n "$NS" --for=condition=Ready pod/web pod/client --timeout=120s >/dev/null \
  || { echo "pods never became Ready"; kubectl delete ns "$NS" --wait=false; exit 1; }

# 1 — baseline connectivity (otherwise later "blocked" would be meaningless)
[ "$(probe)" = "200" ] && ok "baseline: client -> web reachable" \
                       || { bad "baseline: client cannot reach web at all"; kubectl delete ns "$NS" --wait=false; exit 1; }

# 2 — default-deny ingress: the policy Flannel would silently ignore
kubectl apply -n "$NS" -f - >/dev/null <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
spec:
  podSelector: {}
  policyTypes: [Ingress]
EOF
sleep 3
[ "$(probe)" = "200" ] && bad "default-deny: traffic STILL flows (CNI not enforcing!)" \
                       || ok "default-deny: traffic blocked (enforcement is real)"

# 3 — scoped allow: only pods labeled app=client, only port 80
kubectl apply -n "$NS" -f - >/dev/null <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-client-to-web
spec:
  podSelector:
    matchLabels:
      app: web
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: client
      ports:
        - protocol: TCP
          port: 80
EOF
sleep 3
[ "$(probe)" = "200" ] && ok "scoped allow: client -> web restored through the hole" \
                       || bad "scoped allow: traffic still blocked (policy not matching)"

kubectl delete ns "$NS" --wait=false >/dev/null 2>&1
echo "== summary: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
