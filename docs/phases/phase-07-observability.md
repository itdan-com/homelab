# Phase 7 — Observability for the platform AND the agent

**Goal (completion-only per ADR-001):** kube-prometheus-stack already landed in Phase 3 — this phase completes the stack: Loki for logs (+ Tempo for traces if budget allows), the four dashboards (cluster, app, LLM cost from the ai-gateway's token metrics, Claude actions), and Alertmanager wired to `#claude-alerts` for anomalies.

**Status:** Not started. Blocked on Phase 6.

---

## High-level outline

1. ~~Install `kube-prometheus-stack`~~ — **moved to Phase 3 (ADR-001).** Verify it's healthy and re-tune retention if disk pressure appeared.
2. Add Loki for logs + a log shipper (Promtail or Vector).
3. Add Tempo for traces + OpenTelemetry instrumentation on the control-plane Claude.
4. Build Grafana dashboards:
   - **Cluster health** (CPU, memory, pod counts, restarts).
   - **App metrics** (request rate, latency, error rate per service).
   - **LLM cost** (tokens/sec, $/min, per-user attribution — sourced from LiteLLM + Langfuse).
   - **Claude actions** (actions/hr, MCP-server-invocation rate, grant request latency, denial rate, kill-switch events).
5. Wire Alertmanager → Slack `#claude-alerts`.
6. Define anomaly rules:
   - Secret reuse (same key appears in two distinct namespaces or files).
   - Abnormal action rate (agent doing > N actions/min).
   - Unexpected namespaces touched.
   - Kill-switch flips (every flip pages immediately).

## Open questions to resolve at the start

- Storage backend for Loki and Tempo: local PVC on k3d, or MinIO from Phase 5? **Recommendation: MinIO** — exercises the object-storage layer and is cloud-portable.
- Retention windows: 7 days metrics? 14 days logs? 30 days traces? Constrained by WSL2 disk.
- Anomaly detection style: Prometheus recording rules + alert rules, or a dedicated anomaly tool? Stick with PromQL for MVP.

## Phase exit criteria

- All 4 Grafana dashboards populated with real data from a running platform.
- An induced anomaly (e.g. simulated secret reuse) fires an alert in `#claude-alerts` within 1 min.
- A Tempo trace shows a Claude action's full call graph: prompt → Sentinel grant → MCP call → upstream API → response.
- Disk usage stays within budget at the chosen retention windows.
- `STATUS.md` updated.

## Notes captured during execution

- (empty)
