# Phase 8 — Observability for the platform AND the agent

**Goal (completion-only per ADR-001):** kube-prometheus-stack already landed in Phase 3 — this phase completes the stack: Loki for logs (+ Tempo for traces if budget allows), the four dashboards (cluster, app, LLM cost from the ai-gateway's token metrics, Claude actions), and Alertmanager wired to `#claude-alerts` for anomalies.

**Status:** **IN PROGRESS from 2026-08-04** (Phase 7 build-complete). Item 2 (Loki + shipper) built and deploying. (Renumbered 7→8 on 2026-07-31 when Airlock became Phase 7.)

---

## High-level outline

1. ~~Install `kube-prometheus-stack`~~ — **moved to Phase 3 (ADR-001).** Verify it's healthy and re-tune retention if disk pressure appeared.
2. ~~Add Loki for logs + a log shipper~~ — **DONE 2026-08-04.** Loki
   single-binary + Grafana Alloy DaemonSet, ingesting all 12
   namespaces, 14-day retention, bounded label set, Grafana datasource
   provisioned. First finding within ten minutes: platform-wide inotify
   exhaustion (see notes).
3. Add Tempo for traces + OpenTelemetry instrumentation on the control-plane Claude.
4. Build Grafana dashboards:
   - ~~**Cluster health**~~ — **DONE 2026-08-04**, as "Platform
     health": restarts, pod phases, requests-vs-allocatable (requests,
     because that is what blocks a deploy), and two Loki panels. Eight
     panels deliberately, not forty.
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

- **2026-08-04 (item 2, Loki):** Alloy over Promtail (Promtail is
  EOL); single-binary Loki (distributed mode is a dozen components for
  a scale this platform will not reach, and `simple-scalable` is a
  values change over the same storage layout). Storage is the
  cloud-parity line — filesystem here, s3/gcs by one value, same code
  path, which is why MinIO was rejected: it would add a service to
  prove a path Loki already takes.
- **2026-08-04 — two failures I caused by wiring Slack in before it
  existed, and the rule that came out of it.** `slack-mcp`'s
  `argo.yaml` promised a `secrets.enc.yaml` that was never created, so
  the app failed to RENDER; its Service was therefore absent, so
  `sentinel-proxy`'s HTTPRoute reported `ResolvedRefs=False` and the
  app went Degraded. Fixed at the root: the chart now renders NOTHING
  without a credential (an unconfigured server syncs as an empty
  release rather than half-deploying a pod pointing at a missing
  Secret), and the proxy's slack upstream is commented out until the
  server exists. **The rule: declaring a dependency before the thing
  exists does not defer the failure, it moves it somewhere less
  obvious** — here, from "Slack is not set up" to "the enforcement
  proxy is degraded", which is a far more alarming sentence for a
  problem that is neither.
- **2026-08-04 — Loki paid for itself in ten minutes.** Its first real
  query turned a backlog item filed as "harmless cosmetic" into
  measured evidence: 460+ `fsnotify: too many open files` lines per
  hour across eight namespaces, with `fs.inotify.max_user_instances`
  at its 128 default. A process that cannot create a watcher cannot
  notice a file change — which is how config reloads and cert
  rotations work. Nothing is visibly broken, which is precisely the
  profile of something that breaks at the next rotation. **The
  transferable point: one warning looks like noise; the same warning
  counted across a platform is a finding. That difference IS the phase.**
- **2026-08-04:** a permanently-degraded app is worse than a missing
  one, because a dashboard that is always red is one nobody reads.
  That is the reason the upstream is commented out rather than left
  dangling with a note.

- (empty)
