# ADR-001 — Re-plan: gateway swap, metrics-first autoscaling, Control-Plane v0

**Date:** 2026-07-25 · **Status:** Accepted (owner-approved in session)

## Context

Owner restated the end goal: a safe sandbox where **one Claude plane**
can run and scale a Kubernetes AI platform on the 64 GB desktop, learn
production-grade patterns hands-on, then **package and open-source the
result** (revenue optional upside). Assessment of the built state
(Phases 1–2 complete and well-aligned) surfaced four plan defects:

1. **LiteLLM judged not production-grade** by the owner; our own
   operational record agrees (OOMKill at 1 Gi serving one model;
   prisma query-engine baked into root-only `/root` forcing
   hardened-root; rolling `main-stable` image tag).
2. **Sequencing bug:** Phase 3's KEDA work needs Prometheus (the
   phase doc itself says the signal comes "from Prometheus"), but
   Prometheus was scheduled in Phase 7.
3. **The goal's magic moment — one Claude plane operating the
   platform — sat at Phase 6**, behind the largest build (Sentinel),
   on a project that had already idled 7 weeks.
4. A **silent outage** (k3s rewrote CoreDNS `NodeHosts` and severed
   the Ollama path while every pod showed healthy) proved docs can
   drift from reality; sessions need a liveness gate.

## Decisions

1. **Phase 2.5 (new): replace LiteLLM with Envoy AI Gateway** (CNCF
   Envoy Gateway's AI extension). Rationale: production data plane +
   Gateway API (both transferable skills), CRD-based config
   (GitOps-native for Phase 4), token-aware rate limiting and token
   metrics (the Phase 3 KEDA signal), and **reuse: Envoy `ext_authz`
   later becomes the Sentinel enforcement point** (Phase 5.5). Risk:
   pre-1.0 — timebox the spike to one session; fallback **Bifrost**
   (single Go binary, OpenAI-compatible, virtual-key-style
   governance). Chart is named `catalog/ai-gateway/` either way —
   implementation stays swappable.
2. **Phase 3 becomes metrics-first:** kube-prometheus-stack core
   moves from Phase 7 into Phase 3; KEDA targets the new gateway's
   metrics; the k6 profile models bursty AI traffic.
3. **Phase 4.5 (new): Control-Plane v0** — a dedicated operator
   Claude Code instance with read-only cluster access and a PR-only
   fine-grained GitHub token. Scale/stop/start/onboard/rollback all
   happen as PRs; the owner's merge is the approval gate; ArgoCD
   applies. **Sentinel-rule amendment:** "Sentinel non-negotiable
   before Phase 6" is narrowed to **"Sentinel non-negotiable before
   any NON-PR external power."** v0 holds no other credential, so
   the paper-trail and human-gate properties fully hold. Full powers
   (Slack ChatOps, MCP catalog, any SaaS) remain behind Sentinel.
4. **Phase 5 slimmed:** Authentik SSO (OpenWebUI + Grafana first) +
   cert-manager TLS. MinIO and Langfuse deploy on demand when
   something needs them, not by default — the gateway swap changes
   the LLM-observability integration anyway.
5. **Phase 5.5 build note:** the Sentinel proxy is built as Envoy
   `ext_authz` calling `/capability-check`, not a hand-rolled proxy.
   Immediately preceded by the planned cluster rebuild: Cilium CNI
   (enforced NetworkPolicy — Flannel doesn't enforce), per-node CPU
   caps, `k3d/coredns-custom.yaml` reapplied.
6. **Session protocol:** liveness gate added as Step 0 of `/resume` —
   verify prior phases' exit criteria still hold before new work.
7. **Open-source track runs in parallel:** LICENSE, README +
   architecture diagram, parameterized paths (`/home/bob` →
   configurable), bring-your-own-age-key doc, an ADR per big
   decision, demo GIF per milestone.

## Consequences

- `catalog/litellm/` is decommissioned in Phase 2.5; its two
  pin-the-image backlog items die with it; the `litellm` Postgres DB
  is dropped during cutover.
- Phase 7 shrinks to completion work: Loki (+ optional Tempo),
  dashboards, Alertmanager rules.
- New phase order: 1 → 2 → **2.5** → 3 → 4 → **4.5** → 5 → 5.5 → 6 →
  7 → 8.
- Scoped-key property survives the swap: gateway consumers each get
  their own SOPS-encrypted key (implementation differs from LiteLLM
  virtual keys; the least-privilege property is preserved).
- Local scaling remains pattern-real, throughput-simulated (one GPU);
  true replica elasticity is proven in Phase 8 (DOKS).
