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

# Read-only cluster eyes + bot GitHub hands, then hand over to Claude.
export KUBECONFIG="$HOME/.config/homelab-operator/kubeconfig"
export GH_TOKEN="$TOKEN"
cd "$REPO/ops/operator"
echo ">>> OPERATOR session: read-only cluster, PR-only GitHub (itdan-homelab-operator[bot])."
echo ">>> No inherited MCP servers (--strict-mcp-config)."
echo ">>> Ask it things like: 'give the ai-gateway a warm spare replica'"
exec claude --strict-mcp-config --mcp-config "$EMPTY_MCP"
