#!/usr/bin/env bash
# Install (or refresh) the operator tick as a systemd USER timer.
# Idempotent. Cloud shape: the identical units run on any Linux VM;
# the two detected values (this checkout's path, the tool PATH) are
# written at install time so the committed units stay host-agnostic
# (ADR-004 construction rule, same pattern as Sentinel's installer).
set -euo pipefail
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

for bin in claude gh kubectl git curl python3; do
  command -v "$bin" >/dev/null || { echo "FATAL: '$bin' not on PATH — install it first." >&2; exit 1; }
done
[ -f "$HOME/.config/homelab-operator/env" ] || { echo "FATAL: operator env missing — SETUP.md §1.4 first." >&2; exit 1; }
TOOL_PATH="$(for b in claude gh kubectl git curl python3; do dirname "$(command -v "$b")"; done | sort -u | paste -sd: -):/usr/bin:/bin"

sed -e "s|__OPS_DIR__|$OPS_DIR|" -e "s|__PATH__|$TOOL_PATH|" \
  "$OPS_DIR/deploy/operator-tick.service" > "$UNIT_DIR/operator-tick.service"
cp "$OPS_DIR/deploy/operator-tick.timer" "$UNIT_DIR/operator-tick.timer"

systemctl --user daemon-reload
systemctl --user enable --now operator-tick.timer

# Keep the user manager (and therefore the timer) alive without an
# open terminal. Harmless if already enabled.
loginctl enable-linger "$USER" 2>/dev/null \
  || echo "NOTE: enable-linger failed — timer runs only while a session exists. Fix: sudo loginctl enable-linger $USER"

echo
systemctl --user list-timers 'operator-tick*' --no-pager
echo "Observations log: ~/.config/homelab-operator/observations.log"
echo "Pause/resume:     systemctl --user stop|start operator-tick.timer"
