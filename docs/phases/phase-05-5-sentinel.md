# Phase 5.5 — Sentinel (security broker, the load-bearing piece)

**Goal:** A trust-domain-separated security broker on the WSL2 host that mints short-lived per-flow capability tokens, gates every MCP call, and provides a one-screen GUI + global kill switch — all unreachable from inside the k3d cluster.

**Status:** Not started. Blocked on Phase 5. **Non-negotiable before Phase 6.**

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

- [ ] HTTP proxy that intercepts MCP traffic. Each request must carry `X-Sentinel-Token` and `X-Flow-Id` headers.
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

- Sentinel proxy placement: sits *on the host* (cluster pods reach out via `host.docker.internal`)? or runs *as a Deployment in cluster* that calls back out to Sentinel's admin API on the host? **Recommendation: proxy-as-Deployment-in-cluster** — more standard pattern, NetworkPolicy is straightforward, and the admin API stays untouched on the host.
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

- (empty)
