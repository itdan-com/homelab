#!/usr/bin/env bash
# Launch the OPERATOR Claude session (Phase 4.5, PR-only).
# Usage: bash ~/homelab/ops/operator/launch.sh
set -euo pipefail

REPO="$HOME/homelab-operator/repo"
set -a; source "$HOME/.config/homelab-operator/env"; set +a

# Sync the operator's private clone to main using a fresh 1h App token
# (the remote URL stays credential-free).
TOKEN=$("$(dirname "${BASH_SOURCE[0]}")/bin/gh-app-token.sh")
git -C "$REPO" fetch -q "https://x-access-token:${TOKEN}@github.com/${GH_REPO}.git" main
git -C "$REPO" reset -q --hard FETCH_HEAD
git -C "$REPO" checkout -qB main

# Read-only cluster eyes + bot GitHub hands, then hand over to Claude.
export KUBECONFIG="$HOME/.config/homelab-operator/kubeconfig"
export GH_TOKEN="$TOKEN"
cd "$REPO/ops/operator"
echo ">>> OPERATOR session: read-only cluster, PR-only GitHub (itdan-homelab-operator[bot])."
echo ">>> Ask it things like: 'give the ai-gateway a warm spare replica'"
exec claude
