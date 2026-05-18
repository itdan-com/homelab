# Homelab Build Status

**Active phase:** Phase 2 — Chat baseline as catalog-pattern Helm charts
**Status:** Phase 1 complete (1.1–1.6 done; 1.7 git primer deferred into `docs/operator-cheatsheet.md`).
**Next action:** Read `docs/phases/phase-02-chat-baseline.md` and decide where to start (likely: `git init` + catalog scaffolding before first chart).
**Last updated:** 2026-05-17

---

## Session protocol

Every new Claude session in this repo:

1. Read `CLAUDE.md` for architecture and principles.
2. Read this file (`STATUS.md`) for "where am I now."
3. Read `docs/phases/phase-NN-*.md` for the active phase's detailed checklist.
4. Execute work, ticking checklist items in the phase doc as completed.
5. Before ending: update this file's `Active phase`, `Status`, `Next action`, `Last updated` fields and prepend to `Recent activity log` below.

`CLAUDE.md` is the map. `STATUS.md` is the cursor. Phase docs are the territory.

---

## Recent activity log

_Append-only. Newest at top. One line per significant event. Date in ISO format._

- 2026-05-17 — Phase 1 closed out. systemd verified pre-enabled; docker-ce 29.5.0 + kubectl 1.32 + helm 3.21 + k3d 5.8.3 installed; Portainer CE 2.39 deployed and wired to the 4-node devlab cluster via the Portainer Agent (CE doesn't have kubeconfig import). Three architecture gaps surfaced and parked in backlog: Flannel→Cilium CNI swap before Phase 5.5, k3d node resource over-reporting, missing `.wslconfig`. Operator cheatsheet written at `docs/operator-cheatsheet.md` covering health checks, log inspection, Slack-diff decision protocol, and recovery.
- 2026-05-16 — Build scaffolding bootstrapped: STATUS.md + 8 phase docs created. "How sessions work" section added to CLAUDE.md.
- 2026-05-16 — Phase 5.5 (Sentinel) inserted into architecture as the load-bearing security broker. Kill switch moved out of the cluster to a systemd unit on the WSL2 host (trust-domain separation).
- 2026-05-16 — Plan expanded to 8 phases (was 6). Service catalog pattern + MCP cross-stack control plane + ChatOps+GitOps approval flow locked in.
- 2026-05-16 — Switched from Docker Desktop to docker-ce; Portainer added as the Docker GUI layer. Previous k3d state lost; needs re-creation.

---

## Backlog (captured mid-work, not yet scheduled)

_Items noticed during a phase that don't belong to the current phase. Promote to a phase doc or a memory file when ready. Delete if no longer relevant._

- **CNI swap before Phase 5.5.** k3s ships Flannel by default, which does NOT enforce NetworkPolicy. Sentinel's MVP requires "MCP servers refuse traffic that did not come through the Sentinel proxy" — that's a NetworkPolicy guarantee. Before Phase 5.5 we must install Cilium or Calico (k3d supports `--k3s-arg "--flannel-backend=none"` + `--k3s-arg "--disable-network-policy"`, then deploy Cilium). Don't deploy any MCP servers without this in place.
- **WSL2 default-gateway IP is fragile.** `host.docker.internal` is aliased to `172.19.80.1` baked into the cluster. WSL2 reboot can change this; cluster would need to be recreated. Mitigations to weigh in Phase 2 prep: mirrored networking mode in `.wslconfig`, or a small systemd helper that patches the CoreDNS configmap on boot.
- **busybox `nslookup` is unreliable for cluster-DNS troubleshooting.** Use `nicolaka/netshoot` with `dig` instead. (Caught during 1.5 when an apparent bug turned out to be busybox returning a different upstream answer.)
- **k3d node resource over-reporting / scheduler overcommit risk.** Each k3d node container has no cgroup limits, so each reports the *whole* WSL2 VM resources (31 GiB RAM, 16 CPUs). K8s scheduler sees 4× that and will happily over-commit. When we recreate the cluster for the CNI swap before Phase 5.5, also set per-node memory/CPU caps in the k3d config (e.g. 7 GiB × 4). Until then, set explicit `resources.requests`/`limits` on every deployed Helm chart and don't rely on scheduler-visible "free" capacity.
- **CLAUDE.md vs reality: `.wslconfig` doesn't exist** on the Windows side. CLAUDE.md states the VM is capped at "32 GB RAM / 8 processors" but no `.wslconfig` is present, so WSL runs at its defaults (~31 GiB / 16 threads on this box). Either create the `.wslconfig` to enforce the documented caps, or update CLAUDE.md to reflect reality. (Owner to decide before Phase 2.)

---

## Blocked / waiting

_Things stuck on external dependencies (cloud credit, approvals, vendor responses, etc.)._

- (none)

---

## Token-economy reminders for working sessions

- One phase per session when possible. Don't drag the whole plan through every conversation.
- Sonnet for executing checklist items; Opus for architectural decisions; Haiku for trivial edits.
- Use `Explore` subagent for any codebase search with > 3 likely targets.
- Don't churn `CLAUDE.md` mid-session — it lives in the prompt cache; stable file = warm cache.
- Use `TaskCreate` for within-session step tracking; the markdown files here are the cross-session source of truth.
