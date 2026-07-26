# Phase 4.5 — Control-Plane v0 (PR-only operator Claude)

**Goal:** The project's magic moment, safely early: a dedicated
operator Claude Code instance that observes the cluster read-only and
changes it ONLY by opening PRs against this repo. The owner's merge
is the approval gate; ArgoCD applies. Start / stop / scale / onboard /
rollback on demand — with zero non-git credentials.

**Status:** **IN PROGRESS — paused 2026-07-26 ~01:15.** 4.5.1 + 4.5.2
done; demo loop 1 (warm spare, PR #1) merged + applied end-to-end.
**Resume point: demo loop 2** — launch the operator
(`bash ~/homelab/ops/operator/launch.sh`) and paste the rollback
prompt from the Notes below. Then loop 3 (onboard), then the GIF.
NOTE: the repo went **PUBLIC** this session (free-tier rulesets
require it; pre-publication history sweep came back clean), and the
"fine-grained PAT" item was superseded by a **GitHub App** — better
on every axis (see Notes).

**Security framing (ADR-001, decision 3):** this phase exists under
the amended rule — Sentinel is non-negotiable before any **non-PR**
external power. v0's only credential is a fine-grained GitHub token
scoped to this one repo (Contents RW + Pull requests RW). It cannot
merge (branch protection requires owner review), cannot write to the
cluster (view-only RBAC), and holds no SaaS credentials. Every action
IS a PR — the paper trail is the mechanism, not an add-on. Anything
beyond PR-authoring waits for Sentinel (Phase 5.5 → 6).

---

## Checklist

### 4.5.1 Guardrails first

- [x] Branch-protect `main`: require 1 review, no self-approval, no
      force-push. The owner taps Merge in the GitHub UI — every PR
      body must lead with a plain-English summary (git-literacy
      contract), diff second.
      *(Ruleset `protect-main`, Active: require PR + 1 approval,
      force-push/deletion blocked. Repo admin on the bypass list —
      deliberate, keeps builder sessions pushing during the build
      era; REMOVING that bypass is the one-click Phase-6 hardening.
      GitHub visibly logs each bypass on push.)*
- [x] ~~Fine-grained PAT~~ **Superseded by a GitHub App** —
      `itdan-homelab-operator` (App ID 4395580), installed on this
      repo only, Contents RW + Pull requests RW. Strictly better than
      a PAT: PRs authored as `itdan-homelab-operator[bot]` (real
      author/approver separation), 1-hour tokens minted per task from
      the App key (no long-lived credential exists), owner kill
      switch = uninstall the App. Key at
      `~/.config/homelab-operator/github-app.pem` (0600, never in
      repo); minting via `ops/operator/bin/gh-app-token.sh`.
- [x] Read-only observation: ServiceAccount bound to the `view`
      ClusterRole, kubeconfig exported for the operator. Verify a
      mutating call is refused (capture the refusal — it's evidence).
      *(SA `platform-control/operator-view`; kubeconfig at
      `~/.config/homelab-operator/kubeconfig`. Captured evidence:
      `configmaps is forbidden: User "system:serviceaccount:platform-
      control:operator-view" cannot create resource "configmaps"` and
      the matching `cannot list resource "secrets"` refusal.)*

### 4.5.2 The operator instance

- [x] `ops/operator/CLAUDE.md` runbook: role, PR-only rule, PR-body
      template (plain English first), namespaces in scope, "no direct
      kubectl writes ever," escalation path = open a GitHub issue.
- [x] Dedicated Claude Code session on the WSL host loading that
      runbook, the PAT, and the read-only kubeconfig.
      *(One-liner: `bash ~/homelab/ops/operator/launch.sh` — syncs the
      operator's OWN clone at `~/homelab-operator/repo`, mints a fresh
      token, pins KUBECONFIG to the view-only one, starts in
      `ops/operator/` so the charter loads.)*

### 4.5.3 The demo loops

- [x] **Scale:** ~~"scale openwebui to 3"~~ re-targeted (OpenWebUI is
      single-replica by design — same RWO/SQLite reality noted in
      phase 3): **"give the ai-gateway a warm spare"** → operator
      raised KEDA `minReplicas` 1→2 → **PR #1** by
      `itdan-homelab-operator[bot]`, exemplary plain-English body,
      one-line diff → owner approved + merged (merge commit) →
      ArgoCD applied → 2/2 warm in ~2 min. DONE 2026-07-26.
- [ ] **Rollback:** "undo that" → revert PR → merge → back to 1.
      *(Resume here. Prompt for the operator: "Actually, undo that
      warm spare for now — revert your PR so the KEDA minimum goes
      back to 1. Same process: propose it as a pull request and I'll
      review it on GitHub." Expect the drain to take ~5 min after
      sync — HPA scale-down stabilization, not a failure.)*
- [ ] **Onboard:** a trivial new catalog service lands end-to-end by
      PR (template chart → PR → merge → ArgoCD discovers it).
- [ ] Record the demo GIF — this is the open-source pitch asset.

## Open questions to resolve at the start

- Does ArgoCD auto-sync on merge fast enough for a live demo, or
  pin a sync-wave/refresh call into the flow?
- PR body template: include rendered-manifest diff (`helm template`
  before/after) or keep values-diff only? Owner readability decides.

## Phase exit criteria

- All three demo loops merged + applied with zero human git typing
  and zero operator credentials beyond the PR-scoped PAT.
- A refused direct write (RBAC denial) captured as evidence.
- Demo GIF recorded and linked from the README.
- `STATUS.md` updated.

## Notes captured during execution

- **2026-07-26 — repo went PUBLIC** (visibility flip + ruleset in one
  move): GitHub Free doesn't enforce branch rules on private personal
  repos; options were Pro ($4/mo) or public — owner chose public.
  Pre-publication sweep FIRST: full-history grep for every known
  secret prefix + key markers = clean; all five `secrets.enc.yaml`
  verified encrypted; age key never tracked. LICENSE (AGPL-3.0),
  README, CONTRIBUTING were already in place — published looking
  intentional.
- **GitHub App over machine account over PAT:** owner vetoed a second
  account (fair — account sprawl); the App gives bot authorship
  (`[bot]` suffix), author≠approver enforcement, 1h self-minted
  tokens, per-repo installation, and uninstall-as-kill-switch. The
  Phase-6 Slack-merge bot will reuse this identity.
- **PR #1 quality bar:** the operator followed the runbook on first
  flight — observed read-only first, one-line semantic change with a
  sensible comment, branch `operator/warm-spare-ai-gateway`, body led
  with plain English and stated what did NOT change. Keep this as the
  reference example.
- **Multi-writer main begins:** builder push got non-fast-forward
  rejected minutes after the bot's merge — pull --rebase before push
  is now the builder norm too. GitHub prints "Bypassed rule
  violations" on every admin direct push: the bypass is visible,
  which is the point.
- **Merge style norm:** always "Create a merge commit" on this repo —
  preserves bot authorship AND the human's merge event as separate
  history. Squash/rebase would blur the two-actor audit trail.
- **Operator hygiene:** its clone lives at `~/homelab-operator/repo`
  (never the builder's checkout); remote URL kept credential-free
  (tokens injected per-fetch/push); GH App creds/env/kubeconfig all
  under `~/.config/homelab-operator/` (0600, outside git).
