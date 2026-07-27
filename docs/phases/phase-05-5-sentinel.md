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

- [ ] Choose stack: Python (FastAPI) or Node (Fastify). Decide on personal preference + WebAuthn library quality. Recommendation: Python + FastAPI + `webauthn` package.
- [ ] Initialize repo subdirectory `sentinel/` (it lives inside `~/homelab` for now; can split later).
- [ ] Set up SQLite migration tooling (Alembic for Python, or Prisma for Node).

### 5.5.2 Data model

- [ ] `flows` table: `id`, `agent`, `started_at`, `ended_at`, `metadata` (JSON).
- [ ] `capability_grants` table: `id`, `flow_id`, `tool`, `granted_at`, `expires_at`, `granted_by`, `revoked_at`.
- [ ] `audit_events` table: `id`, `ts`, `event_type` (request/grant/denial/use/revocation), `flow_id`, `tool`, `actor`, `details` (JSON).

### 5.5.3 Broker API

- [ ] `POST /capability-request` — Claude posts (flow_id, tool, reason). Returns request-id + 202. Pushes to GUI via SSE/WebSocket.
- [ ] `POST /capability-grant` — GUI posts (request-id, ttl, granted_by). Mints token, writes to `capability_grants`, returns token to Claude over pending request channel.
- [ ] `GET /capability-check?token=...&tool=...&flow_id=...` — Sentinel proxy calls; returns allow/deny.
- [ ] `POST /kill` (admin) — global kill: invalidates all unexpired tokens.

### 5.5.4 Sentinel proxy

- [ ] Built as **Envoy `ext_authz`** (ADR-001 — reuse the Phase 2.5 Envoy investment, don't hand-roll a proxy): an Envoy listener in front of MCP traffic whose ext_authz filter calls Sentinel `/capability-check`. Each request must carry `X-Sentinel-Token` and `X-Flow-Id` headers.
- [ ] On every request: call `/capability-check`; forward to MCP server only if allowed.
- [ ] NetworkPolicy in k3d: MCP server pods refuse traffic except from the Sentinel proxy's source IP/identity.

### 5.5.5 One-screen web GUI

- [ ] Active flows list (live).
- [ ] Pending requests panel: shows flow-id, tool, reason, recent actions from the flow. Buttons: **Grant 5m**, **Grant 1h**, **Deny**.
- [ ] Global kill switch button (with confirmation).
- [ ] Recent audit events tail (last 50).

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
- mTLS between Sentinel proxy and Sentinel admin: private CA generated at install time; certs rotate every 90 days.
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
