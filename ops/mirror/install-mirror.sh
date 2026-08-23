#!/usr/bin/env bash
# Install (or refresh) the mirror as a systemd USER timer — no sudo,
# same pattern as ops/operator/install-tick.sh. Idempotent.
set -euo pipefail
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

for bin in git curl; do
  command -v "$bin" >/dev/null || { echo "FATAL: '$bin' not on PATH." >&2; exit 1; }
done

sed -e "s|__OPS_DIR__|$OPS_DIR|" \
  "$OPS_DIR/deploy/homelab-mirror.service" > "$UNIT_DIR/homelab-mirror.service"
cp "$OPS_DIR/deploy/homelab-mirror.timer" "$UNIT_DIR/homelab-mirror.timer"

systemctl --user daemon-reload
systemctl --user enable --now homelab-mirror.timer

# First sync now, so the mirror exists before the first outage rather
# than after it — and so this install fails loudly if it cannot.
"$OPS_DIR/mirror-sync.sh"

systemctl --user list-timers 'homelab-mirror*' --no-pager
echo "Mirror: ~/.local/state/homelab-mirror (repo.git, charts/, tools/, last-sync.txt)"
