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
#   8. cert-manager CRDs, rendered from the chart tarball already
#      vendored in catalog/ (same version pin, no extra downloads).
#      Needed because catalog/argocd ships its own TLS door — a
#      Certificate resource — and helm validates kinds up front; but
#      cert-manager itself deploys via ArgoCD. Classic chicken-and-egg:
#      CRDs are cluster infrastructure (like Cilium), apps stay GitOps.
#   9. ArgoCD (catalog/argocd umbrella, two-pass: CRDs land, then the
#      catalog ApplicationSet activates).
#  10. Operator read-only kubeconfig (view RBAC + minted SA token) at
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

step "1/10 Preflight"
for bin in docker k3d kubectl helm sops; do
  command -v "$bin" >/dev/null || { echo "MISSING: $bin — install it first"; exit 1; }
done
helm plugin list | grep -q secrets || { echo "MISSING: helm-secrets plugin (helm plugin install https://github.com/jkroepke/helm-secrets)"; exit 1; }
[ -f "$AGE_KEY_FILE" ] || { echo "MISSING: age key at $AGE_KEY_FILE (age-keygen -o ...)"; exit 1; }
echo "ok"

step "2/10 k3d cluster '$CLUSTER_NAME'"
if k3d cluster list 2>/dev/null | awk '{print $1}' | grep -qx "$CLUSTER_NAME"; then
  echo "exists — skipping create"
else
  k3d cluster create --config k3d/devlab-cluster.yaml
fi

step "3/10 Cilium CNI $CILIUM_VERSION"
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

step "4/10 Per-node CPU burst ceiling (${NODE_CPU_LIMIT} cpus)"
# Contention cap only — keeps any one node from starving the WSL2
# host. The scheduler-facing budget (allocatable ~2 vCPU/node) is set
# in devlab-cluster.yaml via kubelet system-reserved. docker update
# persists across container restarts; recreates re-run this script.
for node in $(cluster_nodes); do
  docker update --cpus="$NODE_CPU_LIMIT" "$node" >/dev/null && echo "capped: $node"
done

step "5/10 CoreDNS host-gateway override"
kubectl apply -f k3d/coredns-custom.yaml
kubectl -n kube-system rollout restart deploy/coredns >/dev/null
echo "applied (host.docker.internal resolution survives k3s restarts)"

step "6/10 Portainer agent"
kubectl apply -f k3d/portainer-agent.yaml
# A cluster recreate deletes and recreates the k3d docker network;
# an existing Portainer container is left detached from it. Reattach
# so its Kubernetes environment reconnects without human clicks.
if docker ps -a --format '{{.Names}}' | grep -qx portainer; then
  docker network connect "k3d-$CLUSTER_NAME" portainer 2>/dev/null \
    && echo "portainer reattached to k3d-$CLUSTER_NAME" \
    || echo "portainer already attached"
fi

step "7/10 Bootstrap secrets (age key + lab CA)"
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
# ADR-006: Sentinel's out-of-git cluster artifacts (its CA as a Traefik
# clientAuth secret; Prometheus's scrape client cert). Prometheus HARD
# mounts sentinel-prometheus-client (prometheusSpec.secrets), so a
# rebuild that skips this leaves the ENTIRE metrics stack — alerting,
# dashboards, KEDA's scaler signal — stuck in ContainerCreating.
# mint-certs.sh is idempotent and keeps an existing CA (the checkout's
# gitignored sentinel/certs/ is what survives rebuilds, same as the
# lab CA survives via SOPS).
if [ -x sentinel/scripts/mint-certs.sh ] || [ -f sentinel/scripts/mint-certs.sh ]; then
  bash sentinel/scripts/mint-certs.sh >/dev/null \
    && echo "sentinel PKI in place (CA kept if it existed) — cluster secrets injected" \
    || { echo "!! sentinel/scripts/mint-certs.sh FAILED — Prometheus will not start"; exit 1; }
else
  echo "!! sentinel/scripts/mint-certs.sh missing — Prometheus mounts sentinel-prometheus-client and will NOT start without it"
  exit 1
fi

step "8/10 cert-manager CRDs (argocd's own TLS door needs the Certificate kind)"
# Server-side apply: these CRDs exceed client-side apply's annotation
# budget — the same reason the catalog app runs ServerSideApply=true.
# ArgoCD re-applies identical CRDs later and simply becomes their
# manager; `crds.keep` in the catalog chart protects them from prune.
# dependency build first: charts/ tarballs are deliberately gitignored
# (ArgoCD resolves deps from Chart.lock itself) — a fresh clone has none.
# ADR-009 D3: `helm dependency build` reaches chart repos, 4 of 8 of
# which are GitHub Pages — so a rebuild during a GitHub outage needs
# the mirror's tarball cache as a fallback. dep_build tries the
# network, then the cache, then fails honestly.
MIRROR_CHARTS="${MIRROR_ROOT:-$HOME/.local/state/homelab-mirror}/charts"
dep_build() { # chart-dir
  helm dependency build "$1" >/dev/null 2>&1 && return 0
  if [ -d "$MIRROR_CHARTS" ] && ls "$MIRROR_CHARTS"/*.tgz >/dev/null 2>&1; then
    echo "  (chart repos unreachable — falling back to the mirror's tarball cache for $1)"
    mkdir -p "$1/charts"
    cp -u "$MIRROR_CHARTS"/*.tgz "$1/charts/" 2>/dev/null || true
    # helm accepts pre-populated charts/ matching Chart.lock; verify:
    helm dependency build "$1" >/dev/null 2>&1 && return 0
  fi
  echo "FATAL: helm dependency build $1 failed and no usable mirror cache at $MIRROR_CHARTS" >&2
  return 1
}
dep_build catalog/cert-manager
helm template cert-manager catalog/cert-manager -f catalog/cert-manager/values.yaml \
  | awk 'BEGIN{RS="\n---\n"} /(^|\n)kind: CustomResourceDefinition(\n|$)/ {printf "---\n%s\n", $0}' \
  | kubectl apply --server-side -f - >/dev/null
echo "6 CRDs in place (rendered from the vendored chart, version-locked)"

step "9/10 ArgoCD (then GitOps takes the wheel)"
dep_build catalog/argocd
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

step "10/10 Operator read-only kubeconfig"
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
    # Derive the API server from the NAMED k3d cluster, never from the
    # ambient current-context (--minify reads whatever context happens
    # to be active — a bootstrap run pointed elsewhere would silently
    # write wrong credentials; found by the operator itself, issue #8).
    SERVER="$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name=='k3d-${CLUSTER_NAME}')].cluster.server}")"
    if [ -z "$SERVER" ]; then
      echo "FATAL: cluster 'k3d-${CLUSTER_NAME}' not found in kubeconfig — refusing to mint an operator kubeconfig from an unknown server." >&2
      exit 1
    fi
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
