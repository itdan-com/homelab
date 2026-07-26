# Phase 5 — Team enablement layer

**Goal (slimmed per ADR-001; reconciled at kickoff 2026-07-26):** TLS
on `*.lab.local` via cert-manager + a private lab CA, then
**Authentik** as the platform's OIDC identity provider — OpenWebUI and
Grafana behind one SSO identity (Grafana is already live; Phase 3
pulled monitoring forward, so it joins now, not in Phase 7; Portainer
stays later). **MinIO** and the LLM-observability service (Langfuse or
similar) are strictly **on-demand**: triggers documented below,
deployed only when one fires. After this phase a friend or teammate
gets one account and uses the platform end-to-end over HTTPS.

**Why TLS before SSO** (sequencing decision, kickoff): OIDC redirect
URIs and cookies get configured once, against `https://`, instead of
an http→https rework; and WebAuthn requires a secure context —
passkeys are exactly where this platform is headed (Authentik admin
now, Sentinel in 5.5).

**Status:** In progress — kickoff 2026-07-26 (this doc reconciled:
old exit criteria demanded Langfuse/MinIO despite the slimmed goal).

**Kickoff decisions on record:**

- **Authentik confirmed** as the IdP: MIT-licensed core, no user
  limits, production-grade; **Keycloak is the documented fallback** if
  OIDC gets sticky.
- **Pin Authentik ≥ 2025.10**: that release dropped Redis entirely
  (Postgres-only) — one less moving part, and the idle
  `catalog/postgres` was reserved for exactly this. Post-Redis,
  Authentik opens ~50% more Postgres connections — check
  `max_connections` headroom (B1).
- **CA trust targets the Windows box first** — owner confirmed
  2026-07-26 that RDP-from-Mac is the working model; the Windows-box
  browser is the daily browser. Mac CA trust rides with the
  LAN-teammates backlog item (STATUS.md).

---

## Checklist

### A — TLS foundation (cert-manager + lab CA)

- [x] **A1. `catalog/cert-manager/`** — umbrella chart over upstream
  cert-manager (CRDs enabled, resources capped for the WSL2 budget,
  catalog labels) + `argo.yaml`; ArgoCD app appears, Synced/Healthy.
  *(Done 2026-07-26, commit `c0a6623`: pinned v1.21.0; discovered as
  app #7 and Synced/Healthy ~3 min after push, zero commands typed;
  3/3 pods Running, 6 CRDs, 6/6 labels live-verified.)*
- [ ] **A2. Lab CA** — bootstrap self-signed `Issuer` → long-lived CA
  `Certificate` (`lab-local-ca`) → `ClusterIssuer` signing leaf certs
  for `*.lab.local`. Rotation basics recorded in the cheatsheet.
- [ ] **A3. HTTPS path to the browser** — expose the cluster's 443 at
  the host (prefer `k3d cluster edit --port-add`; fallback: documented
  recreate via `k3d/devlab-cluster.yaml`), issue OpenWebUI's cert,
  switch its IngressRoute to `websecure` + `tls.secretName`, keep an
  HTTP→HTTPS redirect.
- [ ] **A4. Trust the CA on the Windows box** — export the CA cert,
  import into the Windows trust store (elevated step: snapshot-first
  rule applies), then `https://openwebui.lab.local:<port>` shows a
  clean padlock, zero warnings. Write the adopter doc for this step.
- [ ] **A5. Consoles get real doors** — IngressRoutes + certs + hosts
  entries for `grafana.lab.local` (and `authentik.lab.local` when B
  lands; `argocd.lab.local` only if B7 lands). README doors table
  updated as each door opens.

### B — SSO (Authentik as OIDC provider)

- [ ] **B1. Database** — `authentik` DB via the postgres chart's
  `extraDatabases`; verify connection headroom for the post-Redis
  connection count.
- [ ] **B2. `catalog/authentik/`** — umbrella over the official
  authentik chart, pinned ≥ 2025.10 (no Redis), SOPS-encrypted secret
  key + bootstrap admin credentials, resources within budget, catalog
  labels, `argo.yaml`; Synced/Healthy.
- [ ] **B3. `authentik.lab.local` over TLS** via the A-stage
  machinery; admin login works in the browser.
- [ ] **B4. OIDC provider + application for OpenWebUI** configured in
  Authentik — prefer whatever is declaratively reproducible for
  adopters (Authentik blueprints if practical; else clickops now,
  export/document after — decide and note here).
- [ ] **B5. OpenWebUI → Authentik** — OIDC env + SOPS client secret; a
  fresh user signs in via Authentik; admin-role mapping verified;
  decision recorded on the local-password path (disable vs. keep as
  break-glass).
- [ ] **B6. Grafana → Authentik** — OIDC config in the monitoring
  chart values; the same identity logs in; role mapping (Viewer
  default) noted.
- [ ] **B7 (stretch). ArgoCD → Authentik** — SETUP.md already promises
  this for Phase 5; do it if A+B land with budget left, else file as a
  Phase 7 carry-in and correct SETUP.md.

### C — Close out

- [ ] **C1. End-to-end proof** — one identity, browser on the Windows
  box: OpenWebUI + Grafana over HTTPS, no warnings; screenshots for
  the README gallery.
- [ ] **C2. Docs converge** — SETUP.md Part 2 (new doors; password
  sources become "your Authentik account" where SSO landed),
  operator cheatsheet (CA rotation, updated access table), README
  doors table final state.
- [ ] **C3. STATUS.md** cursor + activity log updated; memory file if
  a durable lesson emerged.

---

## On-demand triggers (deliberately NOT deployed this phase)

- **Langfuse** deploys when the first of these fires:
  (a) multi-user traffic exists post-SSO and per-user cost attribution
  is wanted; (b) Phase 7 wants request-level LLM traces alongside
  Loki; (c) tiered-model-routing experiments (backlog) need
  eval/replay data. Caveat on record: traces are not retroactive —
  history before deploy-day never exists. Retrofit is cheap (the
  gateway is OTel-native), so deploy earlier only if trace history
  itself matters.
- **MinIO** deploys when something concretely needs object storage:
  OpenWebUI uploads at team scale, Langfuse's object store, or
  artifact hosting.

## Open questions (resolve at the numbered step)

- **A3:** `k3d cluster edit --port-add` vs. documented recreate for
  the 443 host mapping — pick based on what the installed k3d
  supports.
- **B4:** Authentik blueprints (config-as-code) vs. clickops+export —
  prefer the declaratively reproducible path for adopters.
- **B7:** scope call made live, against remaining session budget.

## Phase exit criteria (reconciled 2026-07-26)

- One SSO identity (Authentik) signs into **OpenWebUI and Grafana**;
  onboarding a teammate = creating one Authentik user.
- Every browser-facing `*.lab.local` door serves HTTPS from the lab
  CA with **zero browser warnings on the Windows box** (the daily
  browser).
- Langfuse/MinIO **not deployed**; their triggers documented above.
- SETUP.md, the cheatsheet, the README doors table, and STATUS.md all
  reflect the new doors.

## Notes captured during execution

- 2026-07-26 (A1): upstream cert-manager chart defaults `crds.enabled:
  false` — the flip to `true` is load-bearing, and `crds.keep: true`
  is the safety net (app deletion can't cascade-delete Certificates).
  `startupapicheck` disabled: it's a Helm hook Job, pure noise under
  ArgoCD's own health checks. Labels via `global.commonLabels` render
  unquoted upstream (`tier: dev` not `tier: "dev"`) — same parsed
  values; assert labels with a YAML parser, not grep. dataClass chose
  `internal` over keda's `none`: cert-manager mints/stores private-key
  Secrets even though it ships none.
- 2026-07-26 (kickoff): doc reconciled — old exit criteria (Langfuse
  trace + MinIO upload + Portainer SSO) contradicted the ADR-001
  slimmed goal; replaced with the criteria above. TLS staged before
  SSO. Grafana moved into scope (live since Phase 3 — the outline's
  "when present in Phase 7" predated ADR-001's Prometheus pull).
  Authentik facts verified against upstream: MIT core; 2025.10
  removed Redis (Postgres-only; ~50% more DB connections expected).
  RDP-from-Mac confirmed as the working model → Windows-box CA trust
  first; Mac trust rides with the LAN-teammates backlog item.
