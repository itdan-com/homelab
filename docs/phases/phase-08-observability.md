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
   - ~~**LLM cost**~~ — **DONE 2026-08-04** as "LLM usage and cost":
     tokens in/out, requests, latency percentiles, share by model, and
     the gateway replicas beside the token rate so the two are read
     together. Price table lives as recording rules generated from
     values. **Per-consumer attribution is NOT possible from these
     metrics** — `gen_ai_*` carries model and token type, no user — so
     the dashboard says so rather than implying it; person-level
     attribution is Sentinel's audit log.
   - ~~**Claude actions**~~ — **DONE 2026-08-22** as "Airlock
     activity" (ADR-006 built): the 3am row (kill switch, live grants,
     pending decisions, active policy version) from the broker's new
     /metrics; event-type and denial-by-server rates plus the raw
     audit stream from the Loki copy; and the record's own health
     (sealer backlog, shipping backlog, permanent skips). Waits on the
     owner's sentinel install for the Prometheus half to populate;
     Loki panels populate from the first shipped seal after that same
     install.
5. Wire Alertmanager → Slack `#claude-alerts`. **Rules written
   2026-08-04** (`catalog/monitoring/templates/alerts-platform.yaml`);
   the receiver still needs a Slack webhook, which is an owner
   decision, so alerts currently fire into Alertmanager's UI only.
6. Define anomaly rules:
   - Secret reuse (same key appears in two distinct namespaces or files).
   - Abnormal action rate (agent doing > N actions/min). *(Partially
     served already: ADR-007 D1's velocity forbids act inline at the
     gate, and `sentinel_audit_events` now exposes the windowed rates
     an alert could key on — the rule itself still unwritten.)*
   - Unexpected namespaces touched.
   - ~~Kill-switch flips~~ — **DONE 2026-08-22**:
     `SentinelKillSwitchEngaged` (critical, 1m) on the new
     `sentinel_kill_switch_engaged` gauge, alongside
     `ArgoCDApplicationSyncUnknown` (ADR-009 D5),
     `SentinelMetricsAbsent`, `SentinelAuditShippingStalled` and
     `SentinelAuditShippingSkipped` — ten live rules total.

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
- **2026-08-04 (alert rules) — I wrote "no rules that can never fire",
  then checked, and two of six could not.** `argocd_app_info` and
  `certmanager_certificate_expiration_timestamp_seconds` had ZERO
  series: neither component was being scraped at all. Fixed at the
  cause (ServiceMonitors on both) rather than by deleting the rules,
  because both signals matter — ArgoCD is how everything else gets
  repaired, so an app it cannot reconcile means the platform has
  stopped self-healing while every pod still looks fine; and cert
  expiry is the silent outage on a platform where every door is TLS.
  Now 15 and 7 series respectively.
- **2026-08-04 — ArgoCD is the one component ArgoCD does not manage**
  (chicken-and-egg: `bootstrap.sh` helm-installs it). So a change to
  `catalog/argocd/values.yaml` does NOT self-apply; it needs the same
  `helm secrets upgrade` bootstrap runs, or the next rebuild. Easy to
  forget and it fails silently — the app list simply does not contain
  it.
- **2026-08-04 — a near-miss worth remembering:** appending a second
  `controller:` block under `argo-cd:` in values.yaml would have been a
  DUPLICATE YAML KEY. The parser keeps the last one silently, which
  would have discarded the helm-secrets plugin config the entire GitOps
  path depends on. Merge into existing blocks; never append a key that
  might already exist.
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

- **2026-08-22 (item 4 Airlock activity + ADR-006):** the full record
  is the STATUS activity log and ADR-006's as-built addendum; the
  five facts a future session needs: (1) "seal" and "segment file"
  are different operations — the shipper pushes newly SEALED rows per
  30s tick in the segment LINE format, so Loki's copy carries the
  hash chain; (2) rotation now 409s rather than prune unshipped rows,
  because pruning them would silently mute the divergence alert;
  (3) the push route's gate was PROBED, not assumed — certless
  refused at handshake, wrong-SNI+forged-Host 421, query API 404,
  secret-deleted fails CLOSED (see push-ingress.yaml's observed
  block); (4) `prometheus-client` reaches every broker route (no
  per-cert authz) — owner decision #11, narrowing named; (5) the
  metrics scrape target correctly shows 404-down until the owner's
  sentinel install ships the /metrics route — `SentinelMetricsAbsent`
  says so in its own description.
- **2026-08-22 — the Cilium IP-lottery recurred and the backlogged
  guard would have caught it in seconds.** Found mid-build: 22 of 32
  scrape targets blackholed (CiliumNode registry stale on 2 of 4
  nodes after a reboot, including a real address collision), fixed
  with the documented DS restart, verified back to 12-down — and the
  remaining 12 (kubelet ×3 nodes, apiserver) turned out to be DOWN
  FOR THE ENTIRE 3-DAY RETENTION WINDOW: a separate, older breakage
  nobody noticed because no rule watches those jobs. Backlogged.
