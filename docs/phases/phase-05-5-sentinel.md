# Phase 5.5 — Sentinel (security broker, the load-bearing piece)

**Goal:** A trust-domain-separated security broker on the WSL2 host that mints short-lived per-flow capability tokens, gates every MCP call, and provides a one-screen GUI + global kill switch — all unreachable from inside the k3d cluster.

**Status:** ENTRY CRITERIA MET 2026-07-27 (see Notes) — ready for 5.5.1. **Non-negotiable before any non-PR external power** (ADR-001 wording — the PR-only Control-Plane v0 of Phase 4.5 may precede this; nothing else may).

**Entry criteria (do immediately before this phase):** ✅ DONE 2026-07-27 — cluster rebuilt from `k3d/devlab-cluster.yaml` with **Cilium** CNI 1.19.6 (kube-proxy kept — DOKS parity), per-node CPU caps (kubelet `system-reserved` + docker ceilings), `k3d/coredns-custom.yaml` + `k3d/portainer-agent.yaml` reapplied by `bootstrap.sh`. The rebuild doubled as the from-git disaster-recovery proof AND the headless-SSO-assembly proof (`scripts/sso-dance.sh` 7/7). Premise correction recorded in Notes: k3s's embedded kube-router controller was already enforcing basic v1 NetworkPolicy — Cilium's case is Hubble per-flow verdict evidence, L7/identity policy headroom, and cloud parity, not "deny was a no-op".

---

## Architecture summary

Sentinel is a systemd unit on the WSL2 host. The k3d cluster has no path to its admin API (one-way trust). Cluster pods call Sentinel's *proxy* (for MCP traffic); they never call Sentinel's *admin* surface. See:

- `CLAUDE.md` → "Trust-domain separation via Sentinel" overview bullet.
- `CLAUDE.md` → Working principle "Trust-domain separation is non-negotiable."
- Memory file `sentinel-design.md`.
- Memory file `mcp-scoping-requirement.md`.

---

## Tight MVP checklist

### 5.5.1 Tech choice + skeleton

- [x] Choose stack: **Python 3.12 + FastAPI + SQLAlchemy/Alembic on SQLite** (2026-07-27; `py_webauthn` joins at 5.5.6). Boring-and-proven beats novel in the trust anchor; zero new host toolchain (venv + pip, pins frozen in `sentinel/requirements.txt`).
- [x] `sentinel/` initialized (`app/` package, `/healthz` live, README states the trust model). Deliberately NOT a catalog chart — it must survive cluster deletion.
- [x] Alembic wired to app metadata + env-driven `SENTINEL_DB` (`render_as_batch=True` for SQLite); `upgrade head` clean; first real migration is 5.5.2's.

### 5.5.2 Data model

- [x] `flows` table: `id` (client-supplied flow-id, string PK), `agent`, `started_at`, `ended_at`, `metadata` (JSON). *(2026-07-27)*
- [x] `capability_grants` table: `id` (UUID hex — unguessable), `flow_id` (real FK), `tool`, **`token_hash`** (SHA-256, unique — deliberate addition: /capability-check validates hashes; tokens are never stored), `granted_at`, `expires_at`, `granted_by`, `revoked_at`.
- [x] `audit_events` table: `id` (autoincrement — ordering is a feature in a log), `ts`, `event_type` (strict 5-value enum + DB CHECK), `flow_id`/`tool`/`actor` (**no FKs, by design** — the audit log must record unknown/garbage flows; an audit insert that can fail referential integrity is a log an attacker can silence), `details` (JSON).

### 5.5.3 Broker API

**Endpoint-count review (owner ask 2026-07-27): four wasn't enough to close the loop; final surface is nine, split across two listeners, zero speculative.** The doc's four had no token-delivery path, no Deny backend, and no kill-release — so the loop couldn't actually complete. Design decisions locked: HTTP-status-as-verdict on check (200/403, native Envoy ext_authz contract for 5.5.4); claim-once token delivery via poll (the granting human never sees the secret); dedupe on duplicate pending (flow,tool); lazy request expiry (no sweeper); kill = revocation persisted in a table (survives restart); everything under `/v1`; FastAPI `/docs` is the human doc, generated from the code so it can't drift.

Cluster-facing listener (`app/broker.py`, binds the k3d gateway IP; mTLS at 5.5.4):
- [x] `POST /v1/capability-requests` — Claude posts (flow_id, tool, reason, agent). 202 new / 200 dedup-onto-pending; registers the flow on first sight. *(2026-07-27)*
- [x] `GET /v1/capability-requests/{id}` — poll; `granted` first-poll carries the token once, then never again; `denied`/`expired` fail closed. **(the missing delivery path)**
- [x] `GET /v1/capability-check?token&tool&flow_id` — **200 allow / 403 deny** (reason: unknown-token/scope-mismatch/revoked/expired/kill-engaged); every call audited.

Admin listener (`app/main.py`, binds 127.0.0.1 only — unreachable from pods by construction):
- [x] `GET /v1/capability-requests` — pending panel (GUI + curl).
- [x] `POST …/{id}/grant` — mints token (→ requester's poll, never echoed here), 201; 409 if not pending or kill engaged.
- [x] `POST …/{id}/deny` — **the Deny button's backend**; resolves the poll immediately.
- [x] `GET|POST /v1/kill` + `POST /v1/kill/release` — engage revokes all live grants (audited each), refuses new; release resumes (old tokens stay dead). **kill-release was missing entirely.**
- [x] `GET /v1/audit-events` (filterable) + `GET /v1/flows` — the record, for the GUI's other two panels.

### 5.5.4 Sentinel proxy

- [x] Built as **Envoy `ext_authz`** (ADR-001): `catalog/sentinel-proxy` (app #11) — own GatewayClass + EnvoyProxy fleet (so only it holds Sentinel's client identity), Gateway-wide SecurityPolicy `extAuth`. Requests carry `X-Sentinel-Token` + `X-Flow-Id`; the TOOL is never client-declared — Envoy forwards the original path + BODY (`bodyToExtAuth`) and the broker derives `<server>.<tool>` itself (`app/scope.py`). *(2026-07-27)*
- [x] On every request: ext_authz calls the broker's `/v1/ext-authz` (wraps the same check_capability + audit); 200 forwards, else refused at the proxy; `failOpen: false` — dead broker = closed door. **Over mTLS with Sentinel's own CA** (`scripts/mint-certs.sh`), broker listener requires client certs.
- [x] NetworkPolicy: fronted pods allow ONLY their Traefik door + the sentinel-proxy fleet (owning-gateway label pair); bypass attempt → Hubble `Policy denied DROPPED` with named source identity. Pattern lives in `catalog/echo` (the 5.5.4 stand-in upstream; strict proxy-only policy lands with the real mock MCP at 5.5.8).

### 5.5.5 One-screen web GUI

- [x] Active flows list (live) — activity **derived** (live grants + last audit event within `SENTINEL_FLOW_ACTIVE_MINUTES`), because nothing closes a flow yet and `ended_at IS NULL` would mark every flow ever run as active. *(2026-07-27)*
- [x] Pending requests panel: flow-id, tool, reason, agent, countdown, the flow's recent audit actions as context. Buttons: **Grant 5m**, **Grant 1h**, **Deny** (grants disabled while kill is engaged).
- [x] Global kill switch button — two-step inline confirm (revocation is permanent; it must not be one stray click away), plus a **Release** control and a loud engaged-state banner.
- [x] Recent audit events tail (last 50), colour-coded by event type.
- [x] **Beyond the checklist, because the console is a web page:** Host allowlist (anti-DNS-rebinding), Origin check, and a required `X-Sentinel-Console: 1` header on every state-changing route — three independent CSRF layers *below* the 5.5.6 auth that will stack on them. Strict CSP (`default-src 'none'`, no CDN), `textContent`-only rendering (agent-written `reason` strings must never become markup in the page that holds the kill switch), and **server-resolved operator identity** — the `*_by` body fields are gone; `extra="forbid"` rejects them loudly rather than silently ignoring a caller who thinks it is naming the approver. All proven in `tests/test_console_guards.py`.

### 5.5.6 Human auth

- [ ] WebAuthn / passkey registration flow on first run.
- [ ] TOTP fallback (otpauth:// QR for any authenticator app).
- [ ] **No password-only path.** A password by itself is not acceptable — phishing the password phishes the kill switch.

### 5.5.7 Systemd integration

- [ ] Write `/etc/systemd/system/sentinel.service` unit file.
- [ ] `Restart=always`, `User=bob`, logs to journald.
- [ ] `systemctl enable --now sentinel`.

### 5.5.8 End-to-end smoke test

- [ ] Stand up a mock MCP server behind the Sentinel proxy.
- [ ] Without a token: request denied.
- [ ] With request → grant → token: request allowed.
- [ ] After TTL expiry: subsequent request denied.
- [ ] Global kill: pre-granted tokens immediately invalidated; new requests refused.
- [ ] Audit log has 5+ event types recorded.

---

## Deferred to roadmap (do NOT build for MVP)

- Capability profiles (preset bundles like `email-drafter`)
- Trust gradients per namespace (sandbox auto-approves, prod always taps)
- Batched grants (one tap for N invocations)
- Mobile PWA
- Full audit dashboard with charts/filters
- Anomaly detection rules

These layer on after Phase 7 without architectural change.

---

## Open questions to resolve at the start

- ~~Sentinel proxy placement~~ **DECIDED (ADR-001):** proxy-as-Deployment-in-cluster, implemented as Envoy with an `ext_authz` filter calling out to Sentinel's `/capability-check` on the host. NetworkPolicy is straightforward; the admin API stays untouched on the host; one-way trust preserved (the proxy holds no grant-issuing power — it only asks).
- ~~mTLS between Sentinel proxy and Sentinel broker~~ **DONE 5.5.4:** Sentinel mints its OWN CA at install (`scripts/mint-certs.sh` — deliberately not cert-manager: the cluster must never mint a cert the broker trusts). 90-day leaves; rotation = re-run `--rotate` + broker restart. Proxy side: EnvoyProxy `backendTLS.clientCertificateRef` (client half) + BackendTLSPolicy pinning Sentinel's CA + SNI (server half); cluster artifacts injected out-of-git (age-key pattern).
- **Listener split (raised at 5.5.1, decide at 5.5.3):** `/capability-check` must be reachable FROM the cluster (the in-cluster Envoy proxy calls it via the host-gateway IP), while grants/GUI/kill must NOT be. The admin app binds 127.0.0.1 — unreachable from pods by construction (proven 2026-07-27: pod → host-gateway:8400 URLError while loopback answered; positive control Ollama:11434 → 200). The check endpoint therefore needs its own listener bound on the host-gateway address + mTLS — likely a second uvicorn socket or a separate minimal app sharing the DB layer.
- Where do MCP server upstream secrets (OAuth tokens for Gmail, GitHub, etc.) actually live? **Recommendation: encrypted at rest in `/var/lib/sentinel/secrets/`, read only by Sentinel — never mounted into pods.** MCP servers get short-lived service tokens from Sentinel, not the upstream secret.

---

## Phase exit criteria

- Sentinel systemd unit running, restartable, logs healthy.
- Web GUI accessible, WebAuthn or TOTP working.
- A mock MCP server behind the Sentinel proxy refuses unauthenticated traffic and permits authenticated, granted traffic.
- Global kill switch tested: tap → proxy refuses all traffic → un-tap → resumed.
- Audit log captures all 5 event types.
- `STATUS.md` updated.

## Notes captured during execution

- **2026-07-27 — adversarial review of everything built so far; six
  more defects closed.** An independent read of the broker + proxy
  (owner ask: "ensure we continue to find bugs as we go") confirmed the
  token leak found above and surfaced these, each now fixed with a
  regression test:
  - **CRITICAL — the one-time token pickup could be stolen.** Dedupe
    matched on `(flow, tool)` alone, so any caller could name someone
    else's scope, receive *their* `request_id`, and race them to the
    claim the instant the human clicked Grant; the poll authenticated
    nothing. Fixed with a caller-minted `claim_nonce` (hash stored,
    required on poll, wrong nonce → same 404 as unknown id). Same fix
    closes the approval-screen lie: a second asker now gets its own
    card with its own justification instead of silently inheriting the
    first one's.
  - **HIGH — live credentials were being written to journald.** The
    token was a query parameter on `/v1/capability-check`, and uvicorn
    logs full query strings. Now a header.
  - **HIGH — MCP could never have worked through the proxy.**
    Streamable HTTP opens its push channel with a bodiless GET and ends
    with a bodiless DELETE; both denied unconditionally as
    `empty-body`. Now grantable as `<server>.rpc.transport.<verb>` —
    the human still says yes, but a session is possible. This would
    have blocked 5.5.8 and Phase 6.
  - **MEDIUM — authorization and execution could read different
    documents.** Duplicate JSON keys are last-wins in Python and
    first-wins elsewhere, so Sentinel could authorize `say` while the
    upstream ran `delete_repo`. Duplicates and NaN/Infinity refused.
  - **MEDIUM — the kill switch could not be pressed twice.** The sweep
    sat inside `if not ks.engaged`, so a grant that became live while
    engaged could never be cleaned up and came back alive on release.
  - **Reliability:** WAL + `busy_timeout` — two processes share the
    SQLite file and the broker commits per MCP call, so a console read
    could block the audit write whose absence then explained nothing.
  - Plus: `claim` is now an audited event type (the record could not
    say whether a capability was ever picked up); the documented
    `SENTINEL_GRANT_TTL_MINUTES` knob was dead and the 24h ceiling
    existed only for callers bypassing the console (now 60);
    `mint-certs.sh` renews on expiry instead of printing success over a
    broken cert; Swagger UI off (the kill-switch origin runs no CDN JS).
  - **Method note worth keeping:** `Synced/Healthy` in ArgoCD means
    "matches what ArgoCD last fetched", NOT "matches HEAD". A probe
    that only checks status will pass against the previous commit —
    compare `.status.sync.revision` to `git rev-parse HEAD`.
  - **End-state written up as ADR-004** (Proposed): Sentinel is a named
    trust domain with a local (WSL2 host) and a cloud (VPC droplet
    outside DOKS) instantiation, because in DOKS there is no free
    "outside the cluster" and both lazy defaults — in-cluster, or on
    the operator's laptop — break either the trust model or
    availability. Phase 8 gains a sixth deliverable. The ADR also
    ranks the debts deliberately NOT fixed (no tenant scoping, one
    fleet cert, mutable audit log, all-or-nothing revocation, opt-in
    enforcement, no egress policy) by cost-now vs cost-later.

- **2026-07-27 — 5.5.5 done (the console), and a real token leak found
  and closed on the way in.**
  - **Bug found by probing, not reading (owner ask: keep hunting).** A
    header-echo upstream behind the proxy showed the live capability
    token arriving at the backend: `x-sentinel-token: snt_…`. A hostile
    or compromised MCP server could replay it for the rest of its TTL.
    Fixed with an HTTPRoute `RequestHeaderModifier` — route-level header
    mutation runs in the router filter, AFTER ext_authz, so the broker
    still validates the token and the upstream never sees it (re-probed:
    header gone, request still 200). **Cleared in the same probe:** a
    client-forged `x-sentinel-grant-id` WAS overwritten by the auth
    response, so the upstream's identity headers are trustworthy.
  - **The console changed the threat model, so the API changed first.**
    Loopback stops the cluster; it does not stop the operator's own
    browser. Three layers now guard every state-changing route — Host
    allowlist (DNS rebinding), Origin check, and a required
    `X-Sentinel-Console: 1` (a custom header forces a CORS preflight
    that Sentinel never answers). Independent of, and below, 5.5.6 auth.
  - **Actor identity moved server-side** (`app/actor.py`): `granted_by`
    / `denied_by` / `engaged_by` / `released_by` are gone from request
    bodies — an actor a caller can type is a signature anyone can
    forge, and these end up in the canonical record. `extra="forbid"`
    makes old callers fail loudly instead of being silently mis-attributed.
    5.5.6 is now a one-file change: `current_operator()` starts
    returning the verified passkey identity.
  - **`GET /v1/flows?active=true` stopped lying.** It filtered on
    `ended_at IS NULL`, which nothing ever sets — so "active flows"
    meant "every flow ever". Now derived from evidence (live grants,
    last audit event within a window) and enriched with `last_seen` /
    `live_grants` / `pending_requests`.
  - XSS discipline is written into `console.js`'s header as a rule, not
    a habit: every agent-written string (`reason`, tool, flow id) is
    rendered with `textContent`. The console also says **NO CONTACT
    WITH SENTINEL** on a failed poll rather than showing stale state —
    an operator who thinks "nothing is pending" while looking at a dead
    socket is worse off than one who knows they are blind.
  - Battery: 4/4 pytest files; live guard checks (403/400/403); and the
    full console loop end-to-end — agent asks → console panel shows it →
    Grant 5m → agent claims token → **200 through the proxy** → console
    **kill** revokes 2 grants → same call `kill-engaged` → release →
    same call `revoked` (release does not resurrect, proven at the wire).

- **2026-07-27 — 5.5.4 done (the enforcement point is live and it cannot
  be lied to).** Full battery through the REAL data path (pod → alias
  Service → Envoy fleet → mTLS ext_authz → broker → echo): no token →
  403 `missing-token` at the proxy; request→grant(bob, 5m)→claim-once
  poll→ **200 "hello from the platform"**; same token with a body
  invoking a different tool → 403 `scope-mismatch`; same token, wrong
  flow-id → 403 `scope-mismatch`; direct pod→echo bypass → Hubble
  `Policy denied DROPPED` with the source pod NAMED; Traefik door
  unaffected; audit shows request/grant/use/denial for the flow.
  - **Design upgrade over the plan:** owner chose proxy-derived tool
    identity (vs trusting a client header). Implemented BETTER than the
    Lua sketch: EG's `bodyToExtAuth` ships the original body to the
    broker, so derivation lives in the TRUST ANCHOR as pure, pytest-able
    Python (`app/scope.py`) — `<server-from-path>.<params.name>` for
    `tools/call`, `<server>.rpc.<method>` otherwise, everything else
    denies closed. Composite scope also kills cross-server token reuse.
    No Lua, no filter-ordering dependency.
  - **mTLS:** Sentinel's own CA (2y) + 90-day leaves via
    `scripts/mint-certs.sh` (idempotent, detects the gateway IP from the
    docker network, injects `sentinel-ca` ConfigMap + `sentinel-proxy-client`
    Secret out-of-git). Broker requires client certs (`run-broker.sh`,
    proven: host-with-cert 200 / host-without refused / pod-without
    refused). `sentinel-broker.internal` declared in
    `k3d/coredns-custom.yaml` — charts reference the NAME; BackendTLSPolicy
    pins CA + SNI to it.
  - **Latent platform bug found & fixed (the big one):** upstream
    ai-gateway-helm REGENERATES its webhook certs every render → each
    catalog commit rotated the Secret while the controller-patched
    `caBundle` went stale → the `failurePolicy: Fail` pod-mutator then
    blocked EVERY EG-provisioned pod create (fleet rollouts AND KEDA
    scale-ups — silently broken since the first post-rebuild commit;
    surfaced by the sentinel fleet's first pod). Fix in
    `catalog/argocd`: `ignoreDifferences` (cert Secret data + webhook
    caBundle) **plus `RespectIgnoreDifferences=true`** — without the
    latter, syncs still overwrite ignored fields. One controller restart
    re-patched the bundle (fingerprints verified equal); the sentinel
    fleet pod then passed the same webhook, proving the path for KEDA too.
  - EG-CR defaults loop again (Phase 4 lesson under-applied): API server
    stamps `group`/`kind`/`weight` — 3 pin commits; templates now
    comment the rule. Echo consents via ReferenceGrant in its OWN chart;
    ingress allowlist NetworkPolicy pattern (plain v1, DOKS-portable)
    also lives there for future MCP charts to copy.
  - **Phase-6 policy note:** MCP streamable-HTTP opens its server-push
    channel with a bodiless GET — today that denies closed
    (`empty-body`). Decide the SSE-channel scope story when real MCP
    servers land (capability profiles are the likely answer). JSON-RPC
    batches are refused by design (one call, one scope, one audit line).
  - Hubble UI enabled (`k3d/cilium-values.yaml` + live upgrade) — the
    visual flow map over the policy verdicts; demo-asset material.

- **2026-07-27 — 5.5.3 done (broker API, two listeners).** Owner
  paused to sanity-check the endpoint count — right call: the doc's
  four endpoints could not close the loop (no way to deliver the token
  to the requester, no Deny backend, no kill-release). Final surface
  is **nine across two ASGI apps**, still zero speculative:
  - `app/service.py` holds every transition; each writes its audit
    event in the SAME commit (no state without a paper trail). `app/
    schemas.py` Pydantic models double as the `/docs` human docs.
  - **Two listeners, not one** — this is the trust boundary made
    physical: `app/broker.py` (request/poll/check) binds the k3d
    gateway IP for pods; `app/main.py` (grant/deny/kill/audit/flows)
    binds 127.0.0.1. Granting literally has no route on the
    cluster-facing app (live-verified: POST grant on :8401 → 404).
  - Design calls, each to dodge a known production-API trap:
    check returns **HTTP-status-as-verdict** (200/403) so Envoy
    ext_authz consumes it unchanged at 5.5.4; **claim-once** token
    delivery (plaintext lives in `capability_requests` only between
    grant and first poll, then nulled — the granting human never sees
    it); **dedupe** on duplicate pending (flow,tool) so retries don't
    spam the GUI; **lazy expiry** (no background sweeper in MVP);
    **kill = revocation persisted** in `kill_state` (survives a
    process restart — proven).
  - Schema grew: `capability_requests`, `kill_state`, `token_hash`
    (5.5.2), token-delivery columns, and 2 audit types
    (kill_engaged/kill_released → 7 total). Migration `80951989de42`;
    Alembic doesn't diff CHECK constraints, so the enum widening is
    hand-added in both directions (verified reversible).
  - Proof: `tests/test_broker_flow.py` 20/20 (happy path, all 4 deny
    reasons, dedupe, deny-then-409, kill-revokes+blocks+**survives
    restart**, release-keeps-old-dead, all 7 audit types) + a live
    two-listener curl walkthrough. `python3.12-venv` needed `httpx2`
    (not httpx) for the test client — dev-only, in requirements-dev.txt.
  - **`smoke.sh` resolved:** an incomplete `sentinel/smoke.sh` was
    the prior Fable session's real-HTTP smoke check, cut off when the
    session was swapped to Opus mid-item (owner confirmed). Removed as
    a broken stub — superseded by the committed pytest lifecycle + the
    live curl walkthrough. A committed real-HTTP smoke test (boots both
    uvicorn listeners, curls the full loop) is a nice-to-have; redo
    cleanly at 5.5.8 if wanted (backlog).

- **2026-07-27 — 5.5.2 done (data model + first real migration).**
  Migration `d405f45ef0a6` (autogen output inspected line-by-line —
  keep that habit), applied + downgrade/re-upgrade proven. Behavior
  smoke 6/6: JSON round-trip, CHECK rejects junk event_type, **FK
  rejects grants for unknown flows** (needs `PRAGMA foreign_keys=ON`
  per connection — wired as an engine event in `db.py`; SQLite
  defaults it OFF), UNIQUE rejects duplicate token_hash, audit accepts
  unknown flow-ids (no-FK design). Conventions locked in `models.py`'s
  docstring: naive-UTC datetimes everywhere; `metadata` column maps to
  `meta` attribute (declarative reserves the name); grant validity =
  not-revoked AND not-expired AND kill-switch-off. Schema deviation
  from this doc, deliberate: `token_hash` column added now so 5.5.3
  stores digests, never tokens.

- **2026-07-27 — 5.5.1 done (stack + skeleton).** Python 3.12/FastAPI/
  SQLAlchemy/Alembic; `sentinel/` scaffolded with the trust model
  written into `README.md` and `app/main.py`'s module docstring.
  Verified: `alembic upgrade head` clean (env.py wired to app
  metadata, batch mode for SQLite), `/healthz` 200 with a real DB
  round-trip, and the **layer-3 one-way-trust probe**: with Sentinel
  up on 127.0.0.1:8400, a pod reached host Ollama (11434 → 200,
  positive control) but got URLError on 8400 — the admin surface is
  invisible to the cluster by construction. Host gap found: Ubuntu
  needed `python3.12-venv` (apt) before venv creation worked — worth
  a prereq line when Sentinel reaches SETUP.md at 5.5.7. Listener-split
  question for /capability-check added to Open Questions.

- **2026-07-27 — entry criteria executed (the Cilium rebuild + DR drill).**
  Full teardown → `bootstrap.sh` → 10/10 apps Synced/Healthy → gate 5/5,
  `sso-dance.sh` 7/7, `netpol-smoke.sh` 3/3. What the drill caught:
  - **Two pets found and converted.** Envoy Gateway v1.8.1 + Envoy AI
    Gateway v1.0.0 control planes were hand-installed in Phase 2.5 and
    lived only in the old cluster — the platform did NOT fully
    self-assemble until `catalog/envoy-gateway` + `catalog/envoy-ai-gateway`
    made them citizens (catalog is now 10 apps).
  - **Bootstrap chicken-and-egg.** `catalog/argocd` ships its own TLS
    door (a Certificate) since B7, so bootstrap now server-side-applies
    the six cert-manager CRDs (rendered from the pinned chart) before
    installing ArgoCD.
  - **CA continuity works as designed.** `k3d/lab-ca.enc.yaml` (SOPS)
    restored pre-ArgoCD; cert-manager adopted it (rotationPolicy Never);
    SHA256 fingerprint identical before/after — client trust survives.
  - **Premise corrected.** Pre-rebuild `netpol-smoke.sh` PASSED under
    Flannel: k3s's embedded kube-router policy controller enforces basic
    v1 NetworkPolicy. The Cilium swap buys per-flow verdict observability
    (Hubble logged the deny phase as `Policy denied DROPPED` with
    security identities — the Sentinel audit primitive), L7/identity
    policy headroom, and DOKS parity. `--disable-network-policy` also
    removes that embedded controller, so Cilium is now the only enforcer.
  - **Eventual-consistency lesson.** Endpoint programming and policy
    propagation are async; one-shot probes race them (baseline probe
    lost under Cilium after winning by luck under Flannel). Harness
    asserts settled state via retry loops.
  - **k3d + no-CNI is fine:** `k3d cluster create --wait` gates on the
    k3s process, not node Ready; nodes sat NotReady until Cilium landed
    (expected, documented in the cluster yaml header).
  - **Known accepted loss:** Grafana + Prometheus sit on `local-path`
    PVCs inside node containers — metrics history and hand-made Grafana
    state die with a rebuild (dashboards/config are provisioned; backlog
    notes the option to move them to hostPath if history starts to matter).
  - Rebuild artifacts: backups in `~/homelab-data-backups/`
    (pg_dumpall 215 tables + 829M data tar + state snapshot), unused —
    hostPath survival worked; keep until next rebuild proves again.
