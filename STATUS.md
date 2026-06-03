# Homelab Build Status

**Active phase:** Phase 2 — Chat baseline as catalog-pattern Helm charts
**Status:** P2.1–P2.7 + P2.12 done. Postgres is live in the `chat` namespace under the catalog contract; data survives `k3d cluster delete` (proven end-to-end). Cluster YAML now mounts `~/homelab-data` into every node; first SOPS-encrypted secret (postgres password) lives in `catalog/postgres/secrets.enc.yaml`. Approach pivot: hand-rolled instead of Bitnami subchart, with CNPG migration planned for Phase 7. Next batch (P2.8–P2.11) is LiteLLM, OpenWebUI, install + browser test.
**Next action:** P2.8 — write `catalog/litellm/` chart. Wire to Ollama on the host via `host.docker.internal:11434`. Master key SOPS-encrypted following the postgres pattern. Set `catalog.homelab/llm-traffic: true`.
**Last updated:** 2026-06-02

**GitHub remote:** https://github.com/itdan-com/homelab (private). Three commits on `main`.

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

- 2026-06-02 — P2.7 closed out. Cluster YAML now mounts `/home/bob/homelab-data` into every node at `/homelab-data` (the load-bearing change for persistent storage). Approach pivoted mid-task from Bitnami subchart to hand-rolled — Bitnami's `commonLabels` workaround would've been a wart on the catalog contract; hand-rolling fits natively via `_helpers.tpl` and gives more learning value. `catalog/postgres/` shipped: Chart.yaml + values.yaml + Service + Secret + PV + PVC + StatefulSet, all six labels propagating. Lint clean. SOPS-encrypted password (first real encrypted secret in repo). **Persistence proof:** wrote two rows → killed the whole cluster → recreated → reinstalled chart → rows still there with same IDs and timestamps. Documented gotcha: hostPath PVs don't honor fsGroup in k3s; one-time `sudo chown -R 999:999 ~/homelab-data/postgres-<release>` required per release. Also closed STATUS backlog item: canonical Portainer Agent manifest now declarative at `k3d/portainer-agent.yaml`. CNPG migration to operator pattern planned for Phase 7.
- 2026-06-02 — P2.6 closed out. `catalog/README.md` (231 lines) lands the six-label schema (four chart-level annotations + two release-level values for `tier`/`data-class`, enabling sandbox→dev→prod GitOps promotion) and the SOPS-secrets-with-Phase-6-migration-plan. `catalog/_template/` scaffolded with `_helpers.tpl` carrying the label-propagation contract; smoke-tested live (pod went 1/1 Running, IngressRoute live under `traefik.io/v1alpha1`, label selectors return the right deployments). SOPS+age installed (age 1.1.1, sops 3.13.1, helm-secrets 4.6.5); encrypt/decrypt round-trip verified via both `sops` and `helm secrets`. `.sops.yaml` at repo root with `encrypted_regex` so only secret VALUES are encrypted (key names stay diff-readable). `.gitignore` hardened with SOPS plaintext safety net. Trust-gradient design saved to memory.
- 2026-05-17 — Phase 2 groundwork session: P2.1 verified Ollama end-to-end (Windows binding fixed via `setx OLLAMA_HOST`, generation through pod->host.docker.internal->qwen3.5:9b returns in <5s). P2.2 `git init` + first commit. P2.3 declarative cluster config at `k3d/devlab-cluster.yaml`. P2.4 cluster rebuilt — scheduler-visible RAM dropped from 124 GiB to 28.7 GiB (the win). P2.5 Portainer auto-reattached. P2.12 pushed to private GitHub: itdan-com/homelab. Two new operational gotchas captured in commits: k3d memory suffix uses `g` not `Gi`; CoreDNS needs a post-create pod-delete to honor hostAliases (race against upstream cache).
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
- ~~**Save the Portainer Agent manifest** so cluster rebuilds are fully declarative.~~ DONE 2026-06-02 — landed at `k3d/portainer-agent.yaml` (not `catalog/portainer-agent/` because Portainer's agent is cluster infrastructure, not a labeled platform workload).
- **Wrap Portainer container into a `compose.yaml`** so its two-network attachment (`bridge` + `k3d-devlab`) is captured declaratively. Currently a manual `docker network connect` after every Portainer container recreate. Do during Phase 2.
- **CPU limits NOT set on k3d nodes** (only memory caps are). When writing Helm charts, set realistic `resources.requests.cpu` — pods that lie about CPU just get throttled, not OOM-killed, but it's still polite.
- **k3d cluster delete is destructive of in-cluster state.** Once we have stateful workloads (Postgres in P2.7, Langfuse in Phase 5), recreating the cluster means losing data unless we mount host paths. Worth thinking about persistent storage strategy before deploying Postgres.
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
