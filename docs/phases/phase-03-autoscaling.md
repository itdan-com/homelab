# Phase 3 — AI-aware autoscaling

**Goal:** Demonstrate both CPU-based HPA on OpenWebUI and event-driven KEDA scaling on LiteLLM, driven by realistic load patterns.

**Status:** Not started. Blocked on Phase 2.

---

## High-level outline

1. Install `metrics-server` (k3d-friendly variant, accept the self-signed cert flag).
2. Add a `HorizontalPodAutoscaler` resource to the OpenWebUI Helm chart; target CPU at 70%.
3. Install KEDA via Helm into namespace `keda`.
4. Add a KEDA `ScaledObject` targeting LiteLLM, scaling on a more AI-relevant signal — tokens/sec or request queue depth — rather than CPU.
5. Run k6 load tests; capture before/after pod counts; tear down.

## Open questions to resolve at the start

- What's the actual scaling signal for LiteLLM? Token rate (from Prometheus via LiteLLM's metrics endpoint)? Active connections (KEDA HTTP add-on)? Decision driven by what LiteLLM exposes natively.
- Resource caps to prevent runaway scaling on a 32 GB WSL2 budget — set hard upper bounds in the HPA/ScaledObject specs.
- Load profile: realistic AI traffic is bursty (rare requests, long durations). Default k6 ramping won't reflect this; consider a custom JS script.

## Phase exit criteria

- `kubectl get hpa` and `kubectl get scaledobjects` both show healthy autoscalers.
- A k6 load run demonstrably scales pods up and back down within minutes.
- Resource ceilings verified (never exceeds the configured max).
- `STATUS.md` updated.

## Notes captured during execution

- (empty)
