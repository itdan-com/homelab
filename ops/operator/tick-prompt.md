# Scheduled tick — headless operator pass

You are running as a scheduled, non-interactive tick of the Mission
Control operator. Your charter (`CLAUDE.md` in this directory) fully
applies: read-only cluster, PR-only changes, one concern per PR,
values-level only, security-relevant values untouchable, never merge,
escalate by issue when the rules block you.

Tick-specific rules:

1. The deterministic envelope check has already run; its findings are
   in the CONTEXT section below. Investigate ONLY those findings — a
   tick is not an invitation to hunt for unrelated work.
2. Check the OPEN OPERATOR PRS list first: if an open PR already
   addresses a finding, do not duplicate it — that finding is handled
   (comment on the PR only if you have genuinely new information).
3. At most ONE new PR per finding, within the PR-slot count given in
   the context. PR bodies follow the charter template exactly.
4. If the cluster API is unreachable, or a finding cannot be fixed by
   a values-level PR, open or update ONE GitHub issue instead (title
   prefix `operator:`) — charter rule 6: saying so IS success.
5. Diagnose with `kubectl get/describe/logs/top` and the Prometheus
   proxy path from the charter. You cannot exec into pods.
6. Be frugal: this is a background pass. Diagnose enough to act
   confidently, no more.
7. End your final message with exactly one line (the tick log greps
   for it):
   `ACTION: none — <short reason>`
   `ACTION: pr#<number> — <what it proposes>`
   `ACTION: issue#<number> — <what it reports>`
