#!/usr/bin/env bash
# OnFailure= handler for operator-tick.service (ADR-009 D2.6).
#
# DELIBERATELY CREDENTIAL-FREE: the first draft put a Slack webhook
# here, and review killed it — a webhook readable by the operator's
# user is a third long-lived credential in the agent's own trust
# domain and a way to spoof the alert channel. This handler writes a
# loud local marker, full stop; the PUSH notification for platform
# conditions is Alertmanager's job (in-cluster, holds the webhook,
# out of this host's reach).
#
# What lands here: any tick that exits non-zero — github_auth_refused
# (deliberately loud: App revocation must page), a crash in the
# harness itself, or the 900s timeout kill. The calm exit-0 verdicts
# (green, github_unreachable, agent-error) never do.
set -u
STATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/homelab-operator"
printf '%s verdict=UNIT-FAILED operator-tick.service exited non-zero — journalctl --user -u operator-tick.service has the story; if the last logged verdict is github_auth_refused, check the GitHub App before anything else\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATE_DIR/observations.log"
