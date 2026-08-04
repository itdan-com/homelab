# Phase 8 — Observability for the platform AND the agent

**Goal (completion-only per ADR-001):** kube-prometheus-stack already landed in Phase 3 — this phase completes the stack: Loki for logs (+ Tempo for traces if budget allows), the four dashboards (cluster, app, LLM cost from the ai-gateway's token metrics, Claude actions), and Alertmanager wired to `#claude-alerts` for anomalies.

**Status:** **IN PROGRESS from 2026-08-04** (Phase 7 build-complete). Item 2 (Loki + shipper) built and deploying. (Renumbered 7→8 on 2026-07-31 when Airlock became Phase 7.)

---

## High-level outline

1. ~~Install `kube-prometheus-stack`~~ — **moved to Phase 3 (ADR-001).** Verify it's healthy and re-tune retention if disk pressure appeared.
2. Add Loki for logs + a log shipper (Promtail or Vector).
3. Add Tempo for traces + OpenTelemetry instrumentation on the control-plane Claude.
4. Build Grafana dashboards:
   - **Cluster health** (CPU, memory, pod counts, restarts).
   - **App metrics** (request rate, latency, error rate per service).
   - **LLM cost** (tokens/sec, $/min, per-consumer attribution — sourced from the AI gateway's `gen_ai_*` metrics + Langfuse).
   - **Claude actions** (actions/hr, MCP-server-invocation rate, grant request latency, denial rate, kill-switch events).
5. Wire Alertmanager → Slack `#claude-alerts`.
6. Define anomaly rules:
   - Secret reuse (same key appears in two distinct namespaces or files).
   - Abnormal action rate (agent doing > N actions/min).
   - Unexpected namespaces touched.
   - Kill-switch flips (every flip pages immediately).

## Open questions — RESOLVED 2026-08-04

- **Storage backend: filesystem now, object storage by values line.**
  MinIO was the earlier recommendation "to exercise the object-storage
  layer", and it was rejected on inspection: it adds a service and its
  own storage to prove a code path that Loki takes natively anyway.
  Loki's `storage.type` moves filesystem → s3/gcs with the schema
  unchanged, so the cloud swap is one value, and standing up MinIO
  locally would prove only that MinIO works.
- **Retention: 14 days for logs** (`336h`, compactor enabled), against
  3 days / 4 GB for metrics as already deployed, and 20 GiB of PVC.
  Disk is not the constraint here — 394 GB free at the time of writing.
  Sentinel's own audit segments keep **90 days** separately, because
  they are the compliance record and these are operational logs.
- **Anomaly style: PromQL rules**, as proposed. Nothing here needs a
  detector that has to be trained before it is useful.

## Decisions taken during the build

- **Alloy, not Promtail.** Promtail is end-of-life; adopting it now
  would mean adopting a dependency with a known expiry date.
- **Single-binary Loki.** Distributed mode splits it into a dozen
  components for a scale this platform will not reach; the move to
  `simple-scalable` is a `deploymentMode` change over the same storage
  layout, so the upgrade path is real rather than a migration.
- **Labels are kept small and bounded, on purpose.** Loki costs memory
  and query time per unique label COMBINATION, so one unbounded label
  (a pod UID, a request id) turns a healthy index into an unqueryable
  one. High-cardinality fields stay in the log line, where search finds
  them.

## Phase exit criteria

- All 4 Grafana dashboards populated with real data from a running platform.
- An induced anomaly (e.g. simulated secret reuse) fires an alert in `#claude-alerts` within 1 min.
- A Tempo trace shows a Claude action's full call graph: prompt → Sentinel grant → MCP call → upstream API → response.
- Disk usage stays within budget at the chosen retention windows.
- `STATUS.md` updated.

## Notes captured during execution

- (empty)
