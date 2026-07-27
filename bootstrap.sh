#!/usr/bin/env bash
# =============================================================================
# One-command bootstrap: empty Docker host -> self-assembling platform.
#
# What it does (idempotent — safe to re-run):
#   1. Preflight: required tools present.
#   2. k3d cluster from k3d/devlab-cluster.yaml (skipped if it exists).
#      The cluster is born WITHOUT a CNI (Flannel is disabled there in
#      favor of NetworkPolicy-enforcing Cilium) — nodes sit NotReady
#      until step 3, which is normal.
#   3. eBPF mounts on every node container, then Cilium (pinned) via
#      helm. Nodes go Ready here.
#   4. Per-node docker CPU ceilings (k3d's config can cap memory but
#      not CPU; the scheduler-facing half lives in devlab-cluster.yaml
#      as a kubelet system-reserved arg).
#   5. CoreDNS host-gateway override (k3d/coredns-custom.yaml) — the
#      durable fix for reaching host services like Ollama; k3s rewrites
#      NodeHosts on restart, this ConfigMap survives.
#   6. Portainer agent (optional visual management) + reattach an
#      existing Portainer container to the recreated cluster network.
#   7. Bootstrap secrets: SOPS age key -> ArgoCD repo-server Secret,
#      and (if k3d/lab-ca.enc.yaml exists) the lab CA keypair -> so a
#      rebuilt cluster keeps the CA client machines already trust.
#      cert-manager ADOPTS a valid pre-existing CA secret instead of
#      minting a new one (rotationPolicy: Never in the chart).
#   8. ArgoCD (catalog/argocd umbrella, two-pass: CRDs land, then the
#      catalog ApplicationSet activates).
#   9. Operator read-only kubeconfig (view RBAC + minted SA token) at
#      ~/.config/homelab-operator/kubeconfig — the k3d API port is
#      random per cluster create, so this must be re-minted per build.
#
# From there GitOps takes over: ArgoCD reads the repo and converges
# every chart in catalog/ that ships an argo.yaml. This script never
# installs application charts — that is the point.
#
# Requirements before first run:
#   - An age keypair (age-keygen) matching .sops.yaml's recipient, at
#     $AGE_KEY_FILE — or your own fork re-encrypted to your key.
#   - A READ-ONLY deploy key for your fork registered on GitHub, with the
#     private half in catalog/argocd/secrets.enc.yaml (see docs).
#
# Overridable env:
#   CLUSTER_NAME (devlab)  AGE_KEY_FILE (~/.config/sops/age/keys.txt)
#   CILIUM_VERSION (1.19.6)  NODE_CPU_LIMIT (4)
# =============================================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-devlab}"
AGE_KEY_FILE="${AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
CILIUM_VERSION="${CILIUM_VERSION:-1.19.6}"
NODE_CPU_LIMIT="${NODE_CPU_LIMIT:-4}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
cluster_nodes() { docker ps --format '{{.Names}}' | grep -E "^k3d-${CLUSTER_NAME}-(server|agent)-" | sort; }

step "1/9 Preflight"
for bin in docker k3d kubectl helm sops; do
  command -v "$bin" >/dev/null || { echo "MISSING: $bin — install it first"; exit 1; }
done
helm plugin list | grep -q secrets || { echo "MISSING: helm-secrets plugin (helm plugin install https://github.com/jkroepke/helm-secrets)"; exit 1; }
[ -f "$AGE_KEY_FILE" ] || { echo "MISSING: age key at $AGE_KEY_FILE (age-keygen -o ...)"; exit 1; }
echo "ok"

step "2/9 k3d cluster '$CLUSTER_NAME'"
if k3d cluster list 2>/dev/null | awk '{print $1}' | grep -qx "$CLUSTER_NAME"; then
  echo "exists — skipping create"
else
  k3d cluster create --config k3d/devlab-cluster.yaml
fi

step "3/9 Cilium CNI $CILIUM_VERSION"
# k3d nodes are containers: Cilium needs a bpf filesystem and a
# cgroup2 mount inside each one, propagated as shared so the agent
# pod's mount namespace sees them. Idempotent — mountpoint guards.
for node in $(cluster_nodes); do
  docker exec "$node" sh -c '
    mountpoint -q /sys/fs/bpf || mount bpffs -t bpf /sys/fs/bpf
    mount --make-shared /sys/fs/bpf
    mkdir -p /run/cilium/cgroupv2
    mountpoint -q /run/cilium/cgroupv2 || mount -t cgroup2 none /run/cilium/cgroupv2
    mount --make-shared /run/cilium/cgroupv2' \
    && echo "mounts ok: $node"
done
helm repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
helm repo update cilium >/dev/null 2>&1 || true
if ! helm status cilium -n kube-system >/dev/null 2>&1; then
  helm upgrade --install cilium cilium/cilium --version "$CILIUM_VERSION" \
    -n kube-system -f k3d/cilium-values.yaml --wait --timeout 8m
else
  echo "cilium release exists — ensuring desired version/values"
  helm upgrade cilium cilium/cilium --version "$CILIUM_VERSION" \
    -n kube-system -f k3d/cilium-values.yaml --wait --timeout 8m
fi
kubectl wait --for=condition=Ready node --all --timeout=180s
echo "all nodes Ready under Cilium"

step "4/9 Per-node CPU burst ceiling (${NODE_CPU_LIMIT} cpus)"
# Contention cap only — keeps any one node from starving the WSL2
# host. The scheduler-facing budget (allocatable ~2 vCPU/node) is set
# in devlab-cluster.yaml via kubelet system-reserved. docker update
# persists across container restarts; recreates re-run this script.
for node in $(cluster_nodes); do
  docker update --cpus="$NODE_CPU_LIMIT" "$node" >/dev/null && echo "capped: $node"
done

step "5/9 CoreDNS host-gateway override"
kubectl apply -f k3d/coredns-custom.yaml
kubectl -n kube-system rollout restart deploy/coredns >/dev/null
echo "applied (host.docker.internal resolution survives k3s restarts)"

step "6/9 Portainer agent"
kubectl apply -f k3d/portainer-agent.yaml
# A cluster recreate deletes and recreates the k3d docker network;
# an existing Portainer container is left detached from it. Reattach
# so its Kubernetes environment reconnects without human clicks.
if docker ps -a --format '{{.Names}}' | grep -qx portainer; then
  docker network connect "k3d-$CLUSTER_NAME" portainer 2>/dev/null \
    && echo "portainer reattached to k3d-$CLUSTER_NAME" \
    || echo "portainer already attached"
fi

step "7/9 Bootstrap secrets (age key + lab CA)"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl create secret generic helm-secrets-age -n argocd \
  --from-file=key.txt="$AGE_KEY_FILE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "secret helm-secrets-age in place"
if [ -f k3d/lab-ca.enc.yaml ]; then
  kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  sops -d k3d/lab-ca.enc.yaml | kubectl apply -f - >/dev/null
  echo "lab CA restored — client machines keep their existing trust"
else
  echo "no k3d/lab-ca.enc.yaml — cert-manager will mint a fresh CA (trust it once per SETUP.md)"
fi

step "8/9 ArgoCD (then GitOps takes the wheel)"
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

step "9/9 Operator read-only kubeconfig"
if [ -f k3d/operator-view-rbac.yaml ]; then
  kubectl apply -f k3d/operator-view-rbac.yaml >/dev/null
  TOKEN=""
  for _ in $(seq 1 30); do
    TOKEN="$(kubectl get secret operator-view-token -n platform-control -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
    [ -n "$TOKEN" ] && break
    sleep 2
  done
  if [ -n "$TOKEN" ]; then
    OP_DIR="$HOME/.config/homelab-operator"
    mkdir -p "$OP_DIR"
    SERVER="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
    CA_DATA="$(kubectl get secret operator-view-token -n platform-control -o jsonpath='{.data.ca\.crt}')"
    cat > "$OP_DIR/kubeconfig" <<KCFG
apiVersion: v1
kind: Config
clusters:
  - name: $CLUSTER_NAME
    cluster:
      server: $SERVER
      certificate-authority-data: $CA_DATA
users:
  - name: operator-view
    user:
      token: $TOKEN
contexts:
  - name: operator-view@$CLUSTER_NAME
    context:
      cluster: $CLUSTER_NAME
      user: operator-view
current-context: operator-view@$CLUSTER_NAME
KCFG
    chmod 600 "$OP_DIR/kubeconfig"
    echo "minted $OP_DIR/kubeconfig (view-only; k3d API port changes per build, so this regenerates every run)"
  else
    echo "WARN: operator-view-token never populated — mint the kubeconfig manually (SETUP.md §1.5)"
  fi
else
  echo "k3d/operator-view-rbac.yaml not present — skipping (operator optional)"
fi

cat <<'DONE'

Bootstrap complete. ArgoCD now converges every chart in catalog/ that
ships an argo.yaml — watch it happen:

  kubectl get applications -n argocd -w

Cilium health:  kubectl -n kube-system exec ds/cilium -- cilium status --brief

UI:   kubectl port-forward -n argocd svc/argocd-server 8081:80
      admin / $(kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d)

If Applications show repo auth errors: register the READ-ONLY deploy key
on your GitHub fork (Settings -> Deploy keys) matching the private key in
catalog/argocd/secrets.enc.yaml.
DONE
