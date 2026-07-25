# Phase 3 — AI-aware autoscaling (metrics-first)

**Goal:** kube-prometheus-stack core installed (pulled forward from
Phase 7 per ADR-001 — KEDA's Prometheus scaler and this phase's
signals need it), CPU-based HPA on OpenWebUI as the baseline, KEDA
scaling the AI gateway on an AI-relevant signal, and a bursty k6 load
run demonstrating both — evidenced in Grafana.

**Status:** Not started. Blocked on Phase 2.5 (gateway swap) — KEDA
targets the new gateway's metrics, so don't build against LiteLLM's.

---

## High-level outline

1. Verify the bundled metrics-server (`kubectl top nodes` — k3s ships
   it; confirmed working 2026-07-25). No separate install needed.
2. Install kube-prometheus-stack core (Prometheus + Grafana +
   Alertmanager): small retention (WSL2 disk), explicit resource
   requests/limits on every component.
3. Scrape the ai-gateway metrics endpoint (ServiceMonitor); confirm
   token/request series land in Prometheus.
4. HPA on OpenWebUI: CPU target 70%, min 1 / max 3.
5. Install KEDA into namespace `keda`; ScaledObject on the
   ai-gateway with a Prometheus scaler — tokens/sec or in-flight
   requests — min 1 / max 3 hard ceiling (32 GB WSL2 budget).
6. k6 with a bursty AI-load script (rare requests, long durations —
   not the default ramp); capture before/during/after pod counts and
   the Grafana view of the scale event; tear down load.
7. While touching `_template`: parameterize the service port name
   (`service.portName` in values) — promoted from STATUS backlog.

## Open questions to resolve at the start

- Which exact gateway metric drives KEDA — decided by what the Phase
  2.5 gateway actually exposes (record metric names in its Notes).
- kube-prometheus-stack as a `catalog/` chart wrapper (label
  contract, ArgoCD-discoverable in Phase 4) vs standalone release?
  Leaning catalog wrapper.
- Grafana admin auth pre-Authentik: temporary SOPS-encrypted
  password, replaced by SSO in Phase 5.

## Phase exit criteria

- `kubectl get hpa` and `kubectl get scaledobjects` both show healthy
  autoscalers.
- A k6 load run demonstrably scales pods up and back down within
  minutes.
- Resource ceilings verified (never exceeds the configured max).
- Prometheus is scraping gateway token metrics; a Grafana screenshot
  of the scale event is saved for the open-source README.
- `STATUS.md` updated.

## Notes captured during execution

- (empty)
