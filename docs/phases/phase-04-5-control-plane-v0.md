# Phase 4.5 — Control-Plane v0 (PR-only operator Claude)

**Goal:** The project's magic moment, safely early: a dedicated
operator Claude Code instance that observes the cluster read-only and
changes it ONLY by opening PRs against this repo. The owner's merge
is the approval gate; ArgoCD applies. Start / stop / scale / onboard /
rollback on demand — with zero non-git credentials.

**Status:** Not started. Blocked on Phase 4 (ArgoCD).

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

- [ ] Branch-protect `main`: require 1 review, no self-approval, no
      force-push. The owner taps Merge in the GitHub UI — every PR
      body must lead with a plain-English summary (git-literacy
      contract), diff second.
- [ ] Fine-grained PAT: this repo only, Contents RW + Pull requests
      RW, nothing else, 90-day expiry. Lives only on the WSL host in
      the operator instance's environment — never in-cluster, never
      committed (not even SOPS'd).
- [ ] Read-only observation: ServiceAccount bound to the `view`
      ClusterRole, kubeconfig exported for the operator. Verify a
      mutating call is refused (capture the refusal — it's evidence).

### 4.5.2 The operator instance

- [ ] `ops/operator/CLAUDE.md` runbook: role, PR-only rule, PR-body
      template (plain English first), namespaces in scope, "no direct
      kubectl writes ever," escalation path = open a GitHub issue.
- [ ] Dedicated Claude Code session on the WSL host loading that
      runbook, the PAT, and the read-only kubeconfig.

### 4.5.3 The demo loops

- [ ] **Scale:** "scale openwebui to 3" → operator edits values → PR
      with plain-English summary → owner merges → ArgoCD syncs → 3/3
      Ready.
- [ ] **Rollback:** "undo that" → revert PR → merge → back to 1.
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

- (empty)
