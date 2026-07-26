# Phase 4 — GitOps with ArgoCD app-of-apps

**Goal:** ArgoCD installed and watching the repo via the app-of-apps pattern, so the entire `catalog/` is one declarative root that auto-discovers every service inside.

**Status:** **PHASE COMPLETE 2026-07-25** (third phase closed today).
Exit criteria: five Applications Synced+Healthy ✓; test commit
auto-synced in **50 seconds** ✓ (criterion: 3 min); catalog is now
demonstrably the only deployment path (argo.yaml presence = deploy
switch, prune on = offboarding is a commit too) ✓; bootstrap.sh +
README.md landed (adopter contract) ✓; STATUS updated ✓.

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

- **2026-07-25 — versions:** argo-cd chart **10.2.1** (ArgoCD v3.4.5)
  as `catalog/argocd` umbrella. Repo access: **read-only deploy key**
  (repo-scoped — verify with `ssh -T`: the greeting must name the
  REPO, `Hi user/repo!`; an account-name greeting means it landed in
  the wrong place with account-wide power).
- **CRD chicken-and-egg:** the ApplicationSet CR ships in the same
  release as its CRD and helm validates manifests up front → two-pass
  install behind `gitops.applicationSetEnabled` (bootstrap.sh encodes
  it; committed value is true).
- **Discovery convention:** ApplicationSet git-files generator over
  `catalog/*/argo.yaml` — all fields required (name = helm release
  name, namespace, secrets). Conditional secrets:// value file needs
  `templatePatch` (per-field templating can't add list items).
- **Adoption protections:** `application.instanceLabelKey` set to the
  ArgoCD annotation so the app.kubernetes.io/instance label (immutable
  selector input) is never rewritten; ServerSideApply for the giant
  kps CRDs; prune off until all five reached Synced/Healthy, then on.
- **SOPS bridge:** helm-secrets 4.6.5 + sops 3.13.1 fetched PINNED by
  a repo-server initContainer; age private key enters the cluster as
  the out-of-git bootstrap Secret `helm-secrets-age` (the documented
  tradeoff; Sentinel supersedes for external creds at 5.5).
  `helm.valuesFileSchemes: secrets` enables `secrets://` valueFiles.
- **Preview-method lesson:** `helm template | kubectl diff` MUST pass
  `-n <ns>` — rendered manifests carry no namespace, so the diff ran
  against `default` and showed a phantom brand-new StatefulSet. The
  corrected preview showed exactly the known 2.5.4 postgres values
  drift, which adoption then reconciled (15s Progressing, data
  intact, zero consumers).
- **Controller-defaults diff loop:** AIGatewayRoute stayed OutOfSync
  because the AI-gateway controller stamps `weight`/`priority`/
  `modelsOwnedBy` defaults into live specs. Fix: render the defaults
  explicitly in the chart (explicit > implicit — same rule as the
  labels). Generic lesson for any CRD-heavy chart under GitOps.
- **Stale-failure cache:** after registering the deploy key, the
  repo-server/appset kept serving the cached auth failure; a rollout
  restart of both cleared it. First check `ssh -T` with the actual
  key before debugging ArgoCD itself.
- **Self-management deferred** (per the open question): catalog/argocd
  is helm-CLI-managed; bootstrap.sh owns it. Revisit post-5.5.
