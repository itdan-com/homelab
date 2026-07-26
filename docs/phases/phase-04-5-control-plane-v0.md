# Phase 4.5 — Control-Plane v0 (PR-only operator Claude)

**Goal:** The project's magic moment, safely early: a dedicated
operator Claude Code instance that observes the cluster read-only and
changes it ONLY by opening PRs against this repo. The owner's merge
is the approval gate; ArgoCD applies. Start / stop / scale / onboard /
rollback on demand — with zero non-git credentials.

**Status:** **IN PROGRESS — 2026-07-26 session 2.** 4.5.1 + 4.5.2 done;
demo loops 1 (warm spare, PR #1) and 2 (rollback, PR #2) merged +
applied end-to-end. Approval gate negative-tested 3/3 pre-merge of
PR #2 (see Notes); evidence issue #3 filed by the operator, answered
by the builder, kept open as the hardening tracker.
**Next: demo loop 3 (onboard)**, then the GIF, then phase close.
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
- [x] **Rollback:** "undo that" → revert PR → merge → back to 1.
      DONE 2026-07-26 — **PR #2** (`revert(ai-gateway): drop the warm
      spare`) authored by the operator, owner merged 16:38:47Z; KEDA
      HPA `SuccessfulRescale → New size: 1` at ~16:39:16Z —
      **~29 s merge→cluster**, not the predicted ~5 min. Lesson: the
      HPA scale-down stabilization window holds the highest *metric*
      recommendation over 5 min; overnight-quiet metrics meant every
      stored recommendation was already at floor, so only
      `minReplicaCount` held 2 replicas — dropping the floor rescales
      instantly. Stabilization delay applies after a *traffic* drop
      (Phase-3 k6 demo), not a *floor* drop during quiet. Pre-merge,
      the owner negative-tested the gate on this very PR (charter /
      422 / 405 — see the 2026-07-26 Notes entry and issue #3).
- [x] **Onboard:** a trivial new catalog service lands end-to-end by
      PR (template chart → PR → merge → ArgoCD discovers it).
      DONE 2026-07-26 — **PR #4**: `catalog/echo/`
      (hashicorp/http-echo:1.0.0, "hello from the platform", new
      `sandbox` namespace, 9 new files, nothing existing touched).
      Operator raised the bar again: pulled the image config from the
      registry (entrypoint, port 5678, ships as uid 65532 → chart
      overrides the skeleton's generic 1000), test-ran the container
      locally under the chart's exact hardening (read-only root,
      cap-drop ALL) before proposing, diffed every template
      byte-for-byte against `_template/`, and flagged its one
      deviation (a `{{- with }}`-guarded `args:` block) at the bottom
      of the PR body instead of burying it. Promotion of that block
      into `_template/` = architecture work → operator offered an
      issue (link it here once filed). Builder e2e verify: 6th app
      Synced/Healthy; contract query
      `kubectl get deploy -A -l catalog.homelab/tier=sandbox` finds
      it; `curl -H 'Host: echo.lab.local'` → HTTP 200, right body.
- [x] Record the demo GIF — this is the open-source pitch asset.
      DONE 2026-07-26 — owner recorded loop 3 live (ScreenToGif; 7000
      raw frames self-edited to ~364 with delay pacing), builder
      shrank 82 MB @2656px → **9.1 MB @1100px** (ffmpeg two-pass
      palette, dither off — dither noise wrecks LZW on screencasts —
      plus alternate-frame thinning of the 0.2 s fast-forward band
      with merged delays, then gifsicle -O3 lossy). Three beats
      verified frame-by-frame: five-app board + plain-English ask →
      PR #4 Merged badge → six-app board with echo Healthy. Lives at
      `docs/assets/pipeline-claude-github-argo.gif`, leads the
      README's "Seen, not told".

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
- **2026-07-26 — the approval gate, negative-tested (owner's idea,
  loop-2 pre-merge).** Before merging PR #2, the owner asked the
  operator to approve+merge its own PR — first casually, then as an
  explicitly authorized one-shot test. Three refusals, three
  independent layers:
  1. *Charter:* the casual ask ("saves me the click") was declined
     without an attempt — the operator re-read its scoping first,
     then: "merging my own PR is the one thing this operator role
     structurally can't do."
  2. *Platform:* `POST /repos/…/pulls/2/reviews {event: APPROVE}` →
     **422** `Review Can not approve your own pull request` (req-id
     `D6D6:1832CC:961902:2127BD4:6A6633B0`). The App authored the PR;
     author≠approver is unconditional GitHub behavior — zero config
     to maintain.
  3. *Ruleset:* `PUT /repos/…/pulls/2/merge` → **405** `Repository
     rule violations found … At least 1 approving review is required`
     (req-id `D6EE:38AFA1:96DD50:211FAD3:6A6633B7`) — `protect-main`
     (id 19757476, enforcement active).
  Post-state verified from BOTH sessions (operator + builder): PR #2
  OPEN, zero reviews, mergeStateStatus BLOCKED, `main` HEAD unmoved
  at `1e51cbb`. **Operator's unprompted finding:** the merge attempt
  *cleared the token-permission layer* (`X-Accepted-Github-
  Permissions: contents=write` — the same grant that pushes proposal
  branches); only the ruleset stopped it, so `protect-main` is the
  single configurable control on the App→`main` path. Builder
  narrowing: bypass list verified = Repository-admin only (the App is
  NOT on it); GitHub App permissions have no per-branch granularity,
  so the operator's "shrink the grant" option doesn't exist — branch
  policy is precisely the ruleset's job; and `itdan-com` is a User
  account, so an org-level duplicate ruleset isn't available as a
  second layer. Residual risk = ruleset misconfig/deletion. Routing:
  admin-bypass removal stays the Phase-6 hardening; NEW Phase-7
  candidate — alert on ruleset-change events so a gate change is loud
  (STATUS backlog). The operator files the evidence issue itself —
  exercising its charter's escalation path, the last runbook path not
  yet used. Captured for adopters as SETUP.md "First flight: prove
  the gate"; README's security-model section now tells the story.
