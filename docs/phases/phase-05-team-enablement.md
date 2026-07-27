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

**Status:** In progress — **stage A (TLS foundation) COMPLETE
2026-07-26** (A1–A5) **+ stage B COMPLETE 7/7** (B1–B3 2026-07-26; B4–B7 2026-07-27:
SSO LIVE on OpenWebUI, Grafana, AND ArgoCD — akadmin admin on all
three, bob user/Viewer/readonly, one-click portal tiles; every flow
proven by the headless login-dance harness). Next: **C — close-out**
(C1 proof screenshots for the README, C2 docs converge — SETUP.md's
B7 promise is now KEPT, doors/password tables say "your Authentik
account", cheatsheet notes the manual-helm posture for argocd —
C3 STATUS final).
Kickoff 2026-07-26 reconciled this doc (old exit criteria demanded
Langfuse/MinIO despite the slimmed goal).

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
- [x] **A2. Lab CA** — bootstrap self-signed `Issuer` → long-lived CA
  `Certificate` (`lab-local-ca`) → `ClusterIssuer` signing leaf certs
  for `*.lab.local`. Rotation basics recorded in the cheatsheet.
  *(Done 2026-07-26, commit `582a363`: CA `CN=homelab lab.local CA`
  valid to 2036, ECDSA P-256, key pinned `rotationPolicy: Never`;
  cross-namespace smoke leaf issued in seconds, `openssl verify` OK,
  test residue deleted; cheatsheet gained "TLS: the lab CA".)*
- [x] **A3. HTTPS path to the browser** — expose the cluster's 443 at
  the host (prefer `k3d cluster edit --port-add`; fallback: documented
  recreate via `k3d/devlab-cluster.yaml`), issue OpenWebUI's cert,
  switch its IngressRoute to `websecure` + `tls.secretName`, keep an
  HTTP→HTTPS redirect.
  *(Done 2026-07-26, commit `2b3cd95`: zero cluster surgery — 8443:443
  was already declarative in `devlab-cluster.yaml` since Phase 2.
  Pattern landed in `_template` (chart-owned Certificate + redirect
  Middleware + two-door IngressRoute), adopted byte-identically by
  openwebui AND echo. Verified: served cert chains to the lab CA,
  https 200 both services, http 301→`:8443` both. Gate/docs swept to
  the new URLs.)*
- [x] **A4. Trust the CA on the Windows box** — export the CA cert,
  import into the Windows trust store (elevated step: snapshot-first
  rule applies), then `https://openwebui.lab.local:<port>` shows a
  clean padlock, zero warnings. Write the adopter doc for this step.
  *(Done 2026-07-26: owner imported via `certutil -addstore Root` and
  logged into OpenWebUI over https with a clean padlock — after a
  browser restart (TLS-verdict cache; now documented). Adopter doc =
  SETUP.md "Trusting the lab CA": export → fingerprint-verify →
  import (Win + macOS) → undo commands. Additive op, no snapshot
  needed; reversal path documented.)*
- [x] **A5. Consoles get real doors** — IngressRoutes + certs + hosts
  entries for `grafana.lab.local` (and `authentik.lab.local` when B
  lands; `argocd.lab.local` only if B7 lands). README doors table
  updated as each door opens. Scope is set by ADR-002's exposure
  policy: doors only for browsed **and authenticated** services —
  Prometheus/Alertmanager are unauthenticated by design and stay
  port-forward until Phase 7 puts them behind SSO forward-auth.
  *(Done 2026-07-26, commit `b86e5f9` + doc sweep: chart-local door in
  the monitoring umbrella (cert SAN + 200 on /login + 301 verified on
  the wire), `grafana.ini root_url` set pre-SSO, hosts line added via
  snapshot-first elevated script (backup `hosts.bak-20260726-194415`,
  receipt file, Windows ping → 127.0.0.1). README gained the
  "logins in ten seconds" block (owner ask).)*

### B — SSO (Authentik as OIDC provider)

- [x] **B1. Database** — `authentik` DB via the postgres chart's
  `extraDatabases`; verify connection headroom for the post-Redis
  connection count.
  *(Done 2026-07-26, commit `c5eef5a`: went further than the item —
  new `auth.extraUsers` gives every app its own role (least-privilege
  over superuser sharing), created on fresh init by
  `20-extra-users.sh` with passwords via Secret env, never ConfigMap.
  Live instance mirrored manually (role+DB `authentik`); pod needed
  no restart. Headroom verified live: authentik idles at 10 of
  max_connections=100.)*
- [x] **B2. `catalog/authentik/`** — umbrella over the official
  authentik chart, pinned ≥ 2025.10 (no Redis), SOPS-encrypted secret
  key + bootstrap admin credentials, resources within budget, catalog
  labels, `argo.yaml`; Synced/Healthy.
  *(Done 2026-07-26, commit `c5eef5a`: pinned 2026.5.6, zero redis
  resources in render. ArgoCD app #8 — server+worker Running,
  Synced/Healthy ~2 min after discovery; 215 tables migrated into OUR
  postgres; health 200 cross-namespace; TLS door serving (SAN
  authentik.lab.local, lab-CA issuer) awaiting only the B3 hosts
  line. akadmin bootstrapped headless via AUTHENTIK_BOOTSTRAP_* from
  SOPS.)*
- [x] **B3. `authentik.lab.local` over TLS** via the A-stage
  machinery; admin login works in the browser.
  *(Done 2026-07-26: hosts line via snapshot-first elevated script
  (backup `hosts.bak-20260726-201249`), owner-verified akadmin login
  on the dashboard — embedded outpost healthy (1), and the owner's
  one mistyped password visible as a failed-login event: the audit
  trail works. Doors tables (README/SETUP/cheatsheet) gained the
  Authentik row at wrap-up.)*
- [x] **B4. OIDC provider + application for OpenWebUI** configured in
  Authentik — **blueprints (config-as-code) REQUIRED** per ADR-002:
  SSO must assemble headless on a machine nobody clicked (the cloud
  deploy). Clickops allowed only as exploration; the committed state
  must reproduce from git.
  *(Done 2026-07-27, commit `19c4106`: `templates/oidc-blueprints.yaml`
  renders CM `authentik-blueprints` into the worker via the chart's
  `blueprints.configMaps` hook — provider (confidential, client_id
  `openwebui`, RS256 via the built-in cert, implicit-consent flow,
  strict redirect derived from `oidcClients.openwebui.baseUrl`) +
  application, both `state: present` so git overwrites UI drift.
  Client secret is a SOPS-only leaf → AUTHENTIK_* env var → `!Env` in
  the blueprint; added with `sops set` (in-place, zero plaintext on
  disk). Verified headless: blueprint instance `successful`, provider
  + application via API, discovery doc over the TLS door advertising
  RS256. One rolling-update race found and understood — see note.)*
- [x] **B5. OpenWebUI → Authentik** — OIDC env + SOPS client secret; a
  fresh user signs in via Authentik; admin-role mapping verified;
  decision recorded on the local-password path (disable vs. keep as
  break-glass).
  *(Done 2026-07-27, commits `11c7ca4`+`d65ab29`+`b7de2d5`: one
  canonical issuer (the public :8443 door) for browser AND backend —
  two named local-only shims make it work in-cluster (coredns
  lab.local template zone → 172.18.0.1; combined CA bundle via
  initContainer, REQUESTS_CA_BUNDLE/SSL_CERT_FILE). Blueprint v2:
  groups openwebui-users/-admins + IdP-side policy bindings +
  prefix-filtered roles scope mapping; app-side
  ENABLE_OAUTH_ROLE_MANAGEMENT maps openwebui-admins→admin on every
  login. Redirect URI source-verified and pinned on both sides
  (`/oauth/oidc/login/callback`); PKCE S256. DECISION: local login
  form stays as break-glass for the pre-SSO admin (dan@itdan.com);
  new humans arrive via SSO. Owner-verified in the browser: akadmin
  → admin, bob → user. Model visibility: gateway allowlist IS the
  policy — BYPASS_MODEL_ACCESS_CONTROL=true, verified as
  impersonated bob. The one real bug — provider `grant_types` empty —
  is a note below.)*
- [x] **B6. Grafana → Authentik** — OIDC config in the monitoring
  chart values; the same identity logs in; role mapping (Viewer
  default) noted.
  *(Done 2026-07-27, commits `8537212`+`5c3ecbb`: blueprint v3 —
  grafana provider (grant_types FROM BIRTH), prefix-filtered
  `grafana-*` roles claim, grafana-users/-admins groups + IdP-side
  bindings, strict redirect to Grafana's fixed
  `/login/generic_oauth`. Monitoring umbrella: `auth.generic_oauth`
  with all three URLs on the canonical public door (coredns zone
  resolves in-cluster; native `tls_client_ca` → the door secret's
  own ca.crt — no initContainer needed; both knobs die on cloud/LE);
  `role_attribute_path` grafana-admins→Admin else Viewer; PKCE;
  client secret as GF_ env from a SOPS-fed umbrella Secret,
  render-asserted absent from the ini ConfigMap. BOTH portal tiles
  now launch the apps' OIDC initiation routes — one click = signed
  in. Break-glass parity: Grafana admin form stays. Live-dance
  verified: akadmin→org Admin, impersonated bob→Viewer. Two snags
  captured in notes: the .gitignore `*-secret.yaml` tripwire ate the
  new template; a NEW blueprint file needs discovery, not the
  watcher.)*
- [x] **B7 (stretch). ArgoCD → Authentik** — SETUP.md already promises
  this for Phase 5; do it if A+B land with budget left, else file as a
  Phase 7 carry-in and correct SETUP.md.
  *(Done 2026-07-27, commit `d453103`: blueprint v4 third key —
  provider (grant_types from birth), groups claim under scope_name
  `groups` (ArgoCD's RBAC default), prefix-filtered, with NO profile
  mapping attached (authentik's built-in profile emits ALL groups as
  `groups` — collision avoided by design); argocd-users/-admins +
  bindings; one-click tile via `/auth/login`. ArgoCD umbrella: house
  door `argocd.lab.local` (server.insecure was already true for this
  exact topology), native oidc.config (dex off) with inline rootCA
  (lab CA public cert, local-only), clientSecret $-referenced from
  argocd-secret via SOPS'd configs.secret.extra; RBAC csv
  argocd-admins→admin, argocd-users→readonly, default empty. Applied
  via manual `helm secrets upgrade` (NOT self-managed — Phase 4
  posture). Live-dance verified: akadmin groups=[argocd-admins]
  can-sync=yes; impersonated bob groups=[argocd-users] can-sync=no,
  8 apps visible read-only.)*

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
- **B4:** ~~blueprints vs. clickops~~ RESOLVED by ADR-002
  (2026-07-26): blueprints required.
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

- 2026-07-27 (B7): **Design note that will recur:** authentik's
  BUILT-IN profile scope mapping emits ALL group names as a `groups`
  claim — any provider that needs a filtered groups claim must NOT
  attach profile (ArgoCD: openid+email+groups only), or the
  unfiltered list rides along and mapping-merge order decides the
  winner. ArgoCD deltas from the pattern: applied via manual
  `helm secrets upgrade` (not self-managed; its own NOTES.txt says
  so), oidc rootCA is INLINE PEM in values (public cert, local-only,
  delete on cloud), RBAC csv maps the claim (admins→admin,
  users→readonly, default empty). The B6 commit-count rule paid off
  same-session: 6/6 files verified in the B7 commit. Windows hosts
  line for argocd.lab.local via the snapshot-first elevated script
  (receipt in Downloads).
- 2026-07-27 (B6): **The .gitignore safety net ate a Helm template:**
  `*-secret.yaml` (the plaintext-secrets tripwire) silently excluded
  the new `templates/grafana-oidc-secret.yaml` from `git add <dir>` —
  local `helm template` rendered it (helm ignores git), ArgoCD never
  saw it, Grafana ran with an empty client secret and bounced
  `/login/generic_oauth` → `/login` with nothing in its logs. Fix:
  renamed to `grafana-oidc-env.yaml`; the net stays. **Rule: after a
  commit that adds files, verify the file COUNT in the commit stat.**
  Blueprint delivery nuance confirmed: the worker's file WATCHER
  re-applies changed known files, but a brand-NEW file in the mount
  needs a `blueprints_discovery` pass (hourly schedule; fast-forward
  with `.send()` — B4 playbook). Grafana's `envFromSecret` has no
  checksum: creating/rotating that Secret needs a manual grafana
  restart (documented in values). kube-prometheus-stack full syncs
  run the admission-webhook hook Job — expect minutes, not seconds.
  One-click tiles: `meta_launch_url` → each app's OIDC INITIATION
  route (`/oauth/oidc/login`; `/login/generic_oauth`) — tile click =
  signed in; login pages with break-glass forms remain on direct
  visit. Memberships (runtime data, via API, logged): akadmin →
  grafana-admins, bob → grafana-users.
- 2026-07-27 (B5): **The trap that bit a real login: blueprint/API-
  created OAuth2 providers get an EMPTY `grant_types` allowlist** —
  the admin UI silently preselects authorization_code; config-as-code
  gets no UI defaults. Authentik then rejects every authorize as
  `invalid_request` ("Invalid grant_type for provider" in server
  logs) and OpenWebUI masks it as "email or password incorrect".
  Blueprint now pins `[authorization_code, refresh_token]` — any
  future OIDC client blueprint MUST include grant_types. Debug method
  that found it: reproduce the click headless from a pod (capture the
  302 authorize URL, replay variants to bisect), then read authentik
  server logs — events showed nothing. **Owner-UX lesson: the
  Authentik portal tile is a LAUNCHER, not a login** — OIDC is
  app-initiated; the button lives on the app's login page. **Session
  rule (owner demand after the miss): verify the LIVE flow, not just
  the objects** — the reusable harness (flow-executor login →
  authorize → callback → who-am-I, plus admin impersonation for
  other-user checks) is in the scratchpad scripts and this doc's
  history; secrets pipe via stdin `sh read` into pod env, never
  printed (heredocs REPLACE piped stdin — use `{ secret; script; } |
  sh -c 'read; exec python3 -'`). Minor: our roles-mapping expression
  trips a deprecation warning (`User.ak_groups`) — works on pinned
  2026.5.6, swap accessor at the next authentik bump (backlogged).
  Model visibility: v0.9.x hides all models from role=user unless
  access_grant rows exist or BYPASS_MODEL_ACCESS_CONTROL — policy
  decision: bypass ON, the gateway consumer-key model list in git is
  the allowlist (per-model grants = future tiering knob).
- 2026-07-27 (B4): **Rolling-update race on first blueprint
  delivery:** the discovery tasks enqueued at new-worker boot were
  consumed by the OLD worker — the task queue is Postgres-backed and
  a terminating pod keeps consuming through its grace period — which
  has no CM mount, so both tasks reported "done" with no instance
  created and nothing marked failed. Self-heals at the next scheduled
  discovery; we fast-forwarded with `blueprints_discovery.send()`
  from `ak shell`. A cold bootstrap (the cloud deploy) is immune —
  there is no old worker. Debug path worth keeping: `blueprints_find()`
  in `ak shell` shows exactly what the scanner sees;
  `/api/v3/managed/blueprints/` shows instance status. Also learned:
  `sops set` edits the encrypted file in place honoring the stored
  encrypted_regex — zero plaintext on disk, stricter than the B1
  rule; prefer it when adding single keys. **Two knowns for B5:**
  (1) authentik derives the OIDC issuer from the request's Host
  header — a client hitting the :8443 door sends the port and gets
  `https://authentik.lab.local:8443/...`; my portless test curl got
  the portless issuer. Pick OpenWebUI's provider URL so both fetch
  path and issuer agree. (2) OpenWebUI's BACKEND fetches the
  discovery URL server-side — `authentik.lab.local` does not resolve
  in-cluster and the pod does not trust the lab CA; B5 must choose
  internal service URL vs. CoreDNS rewrite + CA trust and record it.
- 2026-07-26 (B1+B2): **SOPS rule, learned via near-miss:** `sops -e`
  with explicit `--age`/`--encrypted-regex` STILL requires a matching
  creation rule for the input path — encrypting from a /tmp path
  errored, and the shell redirect then truncated the tracked target
  to empty. `git restore` recovered it; all secrets regenerated and
  the live role password rotated (`ALTER ROLE`). **New rule: write
  plaintext AT the destination `secrets.enc.yaml` path, `sops -e -i`
  it, verify `ENC[` + round-trip BEFORE `git add`; never redirect
  sops output over a tracked file.** Also: helm `-f` list semantics
  replace lists wholesale — a SOPS file overriding `auth.extraUsers`
  must carry complete entries, not just passwords. goauthentik chart:
  every leaf under `authentik.*` becomes an `AUTHENTIK_*` env var via
  its rendered Secret (bootstrap_password/token verified in render) —
  no hand-rolled Secret template needed. Postgres StatefulSet does
  NOT restart on Secret/ConfigMap content changes (no checksum
  annotations there — deliberate for a DB); fresh-init-only initdb
  means live instances get mirrored manually, and that's documented,
  not hidden.
- 2026-07-26 (A5): WSL→Windows elevation gotcha: `Start-Process
  -Verb RunAs` through `powershell.exe` interop BLOCKS the WSL shell
  until the UAC dialog is answered — launch it detached/background
  and poll a receipt file instead. The elevated script pattern that
  worked: snapshot first, abort if the file reads back suspiciously
  small (the 2026-07-25 failure mode), append-only + idempotent,
  write a receipt to Downloads. Also: elevated PowerShell writes
  UTF-16 receipts — harmless, but grep/cat show spaced characters.
  Umbrella-door pattern: the umbrella chart owns the door
  (chart-local template, service name stated explicitly), the
  subchart owns the app; `root_url` set before SSO so B6 builds on
  truth.
- 2026-07-26 (A3): Traefik's `redirectScheme permanent: true` emits
  **301**, not 308 — comments/docs corrected after observing the live
  header. Leaf certs are SAN-only (empty subject) — cert-manager
  omits CN when only `dnsNames` are given; browsers require SAN
  anyway, so this is correct, just surprising in `openssl -subject`.
  The 8443:443 host mapping needed no work: declarative in
  `k3d/devlab-cluster.yaml` since the Phase 2 rebuild ("avoid
  clashing with Portainer"), published on the running serverlb all
  along. Liveness gate step 5 updated: https 200 on :8443; :8080
  returning 301 is the redirect working, not a failure. Consumers'
  Certificates apply before cert-manager finishes on a cold
  bootstrap — ArgoCD selfHeal retries absorb it (same no-ordering
  stance as A2; watch it on the next full rebuild).
- 2026-07-26 (A2): the CA chain self-converges with no ordering
  machinery — ClusterIssuer applied before its Secret existed, went
  Ready ~90 s later when the CA Certificate issued (cert-manager
  watches; no sync waves needed, consistent with the contract's
  no-ordering rule). Leaf default confirmed 90 d / renew ~30 d early.
  `rotationPolicy: Never` pinned on the CA (upstream default became
  Always in v1.18 — a rotating trust anchor would strand clients).
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
