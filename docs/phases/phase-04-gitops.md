# Phase 4 — GitOps with ArgoCD app-of-apps

**Goal:** ArgoCD installed and watching the repo via the app-of-apps pattern, so the entire `catalog/` is one declarative root that auto-discovers every service inside.

**Status:** Not started. Blocked on Phase 3.

---

## High-level outline

1. Install ArgoCD via Helm into namespace `argocd`.
2. Configure access to the GitHub repo (deploy key or PAT).
3. Create a root `Application` pointing at `catalog/` with `directory.recurse: true`.
4. Configure the app-of-apps so each child chart becomes its own auto-discovered `Application`.
5. Demonstrate: edit a chart value in git, commit, observe ArgoCD sync within minutes.

## Open questions to resolve at the start

- ArgoCD UI auth: local admin for now? Defer SSO until Phase 5 (Authentik).
- Sync policy: auto-sync from day one for the dev cluster (fast feedback), or manual approval (safer)? Recommendation: auto-sync for the `chat` and `apps` namespaces; manual for anything closer to prod-shaped.
- Self-management: should ArgoCD manage its own Helm chart? (Yes eventually, but easy to footgun — defer.)

## Phase exit criteria

- ArgoCD UI shows the root app and all sub-apps `Synced` + `Healthy`.
- A test commit auto-syncs within 3 minutes.
- The `catalog/` pattern from Phase 2 is now demonstrably the *only* way services get deployed.
- `STATUS.md` updated.

## Notes captured during execution

- (empty)
