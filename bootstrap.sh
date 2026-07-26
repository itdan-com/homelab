#!/usr/bin/env bash
# =============================================================================
# One-command bootstrap: empty Docker host -> self-assembling platform.
#
# What it does (idempotent — safe to re-run):
#   1. Preflight: required tools present.
#   2. k3d cluster from k3d/devlab-cluster.yaml (skipped if it exists).
#   3. CoreDNS host-gateway override (k3d/coredns-custom.yaml) — the durable
#      fix for reaching host services like Ollama; k3s rewrites NodeHosts on
#      restart, this ConfigMap survives.
#   4. Portainer agent (optional visual management).
#   5. SOPS age key -> bootstrap Secret for ArgoCD's repo-server.
#   6. ArgoCD (catalog/argocd umbrella, two-pass: CRDs land, then the
#      catalog ApplicationSet activates).
#   7. From there GitOps takes over: ArgoCD reads the repo and converges
#      every chart in catalog/ that ships an argo.yaml. This script never
#      installs application charts — that is the point.
#
# Requirements before first run:
#   - An age keypair (age-keygen) matching .sops.yaml's recipient, at
#     $AGE_KEY_FILE — or your own fork re-encrypted to your key.
#   - A READ-ONLY deploy key for your fork registered on GitHub, with the
#     private half in catalog/argocd/secrets.enc.yaml (see docs).
#
# Overridable env:
#   CLUSTER_NAME (devlab)  AGE_KEY_FILE (~/.config/sops/age/keys.txt)
# =============================================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-devlab}"
AGE_KEY_FILE="${AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "1/6 Preflight"
for bin in docker k3d kubectl helm sops; do
  command -v "$bin" >/dev/null || { echo "MISSING: $bin — install it first"; exit 1; }
done
helm plugin list | grep -q secrets || { echo "MISSING: helm-secrets plugin (helm plugin install https://github.com/jkroepke/helm-secrets)"; exit 1; }
[ -f "$AGE_KEY_FILE" ] || { echo "MISSING: age key at $AGE_KEY_FILE (age-keygen -o ...)"; exit 1; }
echo "ok"

step "2/6 k3d cluster '$CLUSTER_NAME'"
if k3d cluster list 2>/dev/null | awk '{print $1}' | grep -qx "$CLUSTER_NAME"; then
  echo "exists — skipping create"
else
  k3d cluster create --config k3d/devlab-cluster.yaml
fi

step "3/6 CoreDNS host-gateway override"
kubectl apply -f k3d/coredns-custom.yaml
kubectl -n kube-system rollout restart deploy/coredns >/dev/null
echo "applied (host.docker.internal resolution survives k3s restarts)"

step "4/6 Portainer agent"
kubectl apply -f k3d/portainer-agent.yaml

step "5/6 SOPS age key -> ArgoCD bootstrap secret"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl create secret generic helm-secrets-age -n argocd \
  --from-file=key.txt="$AGE_KEY_FILE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "secret helm-secrets-age in place"

step "6/6 ArgoCD (then GitOps takes the wheel)"
helm dependency build catalog/argocd >/dev/null
if ! helm status argocd -n argocd >/dev/null 2>&1; then
  # First pass with the ApplicationSet disabled: its CRD ships in this
  # same release and helm validates manifests up front.
  helm secrets upgrade --install argocd catalog/argocd -n argocd \
    -f catalog/argocd/values.yaml -f catalog/argocd/secrets.enc.yaml \
    --set gitops.applicationSetEnabled=false --wait --timeout 8m
fi
helm secrets upgrade argocd catalog/argocd -n argocd \
  -f catalog/argocd/values.yaml -f catalog/argocd/secrets.enc.yaml \
  --wait --timeout 5m

cat <<'DONE'

Bootstrap complete. ArgoCD now converges every chart in catalog/ that
ships an argo.yaml — watch it happen:

  kubectl get applications -n argocd -w

UI:   kubectl port-forward -n argocd svc/argocd-server 8081:80
      admin / $(kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d)

If Applications show repo auth errors: register the READ-ONLY deploy key
on your GitHub fork (Settings -> Deploy keys) matching the private key in
catalog/argocd/secrets.enc.yaml.
DONE
