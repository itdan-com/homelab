# Phase 3 — AI-aware autoscaling (metrics-first)

**Goal:** kube-prometheus-stack core installed (pulled forward from
Phase 7 per ADR-001 — KEDA's Prometheus scaler and this phase's
signals need it), CPU-based HPA on OpenWebUI as the baseline, KEDA
scaling the AI gateway on an AI-relevant signal, and a bursty k6 load
run demonstrating both — evidenced in Grafana.

**Status:** **PHASE COMPLETE 2026-07-25** — all seven items, one
session. Exit criteria: autoscalers healthy ✓; k6 burst scaled the
data plane 1→3 in ~85s and back to 1 in ~5min ✓; 3-replica ceiling
never exceeded ✓; Prometheus scraping gateway token metrics ✓;
STATUS.md updated ✓. One open owner action: capture the Grafana
scale-arc screenshot for the README **within the 3-day retention
window** (`kube_deployment_status_replicas_ready{deployment=
"envoy-chat-ai-gateway-8e541394"}` + the token-rate query, ~16:35–16:46).

---

## High-level outline

1. Verify the bundled metrics-server (`kubectl top nodes` — k3s ships
   it; confirmed working 2026-07-25). No separate install needed.
2. Install kube-prometheus-stack core (Prometheus + Grafana +
   Alertmanager): small retention (WSL2 disk), explicit resource
   requests/limits on every component.
3. Scrape the ai-gateway metrics endpoint (ServiceMonitor); confirm
   token/request series land in Prometheus.
4. ~~HPA on OpenWebUI~~ **Re-targeted to the Envoy data plane**
   (2026-07-25): OpenWebUI is single-replica by design — SQLite on an
   RWO PV, `Recreate` strategy; two writers would corrupt it (its own
   chart comments say "needs Postgres-backed state first"). The
   CPU-HPA baseline was demoed via EG-managed `envoyHpa` (CPU 70%,
   1–3, ai-gateway values `autoscaling.mode: hpa`), then deliberately
   superseded by item 5 on the same deployment.
5. Install KEDA into namespace `keda`; ScaledObject on the
   ai-gateway with a Prometheus scaler — tokens/sec or in-flight
   requests — min 1 / max 3 hard ceiling (32 GB WSL2 budget).
   *(Done: `autoscaling.mode: keda` is the committed end state; the
   ScaledObject renders into the controller namespace, next to its
   target workload.)*
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

- **2026-07-25 — item 2 versions:** chart `kube-prometheus-stack@87.19.1`
  (prometheus-operator v0.92.1), Grafana 13.1.1. Umbrella chart at
  `catalog/monitoring/` (release `monitoring`, ns `monitoring`);
  dependency pinned in Chart.yaml, `Chart.lock` committed, tarball
  gitignored (adopters run `helm dependency build`).
- **`.helmignore` umbrella-chart bug (bit us, fixed everywhere):** the
  template's `*.tgz` pattern makes helm's chart LOADER skip
  `charts/*.tgz`, so any chart with dependencies fails install with
  "found in Chart.yaml, but missing in charts/ directory". Latent since
  Phase 2 (no chart had dependencies until now). Fix: rooted `/*.tgz`
  in `_template/.helmignore`, propagated to all charts. Lesson:
  helmignore governs loading, not just packaging.
- **k3s scrape tuning:** kubeControllerManager/kubeScheduler/kubeProxy/
  kubeEtcd scrapes disabled (embedded in the k3s binary — no endpoints);
  result is a clean 25/25 `up` with no permanent TargetDown noise.
- **`serviceMonitorSelectorNilUsesHelmValues: false`** (and the
  podMonitor twin): Prometheus discovers ServiceMonitors from ANY
  release/namespace — required for the catalog pattern (item 3's
  ai-gateway monitor ships with the ai-gateway-adjacent config, not
  inside the monitoring release).
- **Sizing:** retention 3d / 4GB cap, 5Gi local-path PVC (metrics are
  ephemeral ops data — cluster rebuilds lose them by design); explicit
  requests/limits on operator/prometheus/alertmanager/grafana/KSM/
  node-exporter. Measured impact: WSL 3→4 GB used; node memory 5–21%.
- **Umbrella label exception:** subchart resources get the six labels
  via static `commonLabels` duplication (best-effort on sub-subcharts);
  our own templates in this chart use the normal helper. Documented in
  values.yaml.
- **Access until Phase 5 SSO/ingress:** `kubectl port-forward -n
  monitoring svc/monitoring-grafana 3000:80` → http://localhost:3000,
  admin / `sops -d catalog/monitoring/secrets.enc.yaml`.
- **2026-07-25 — items 3–5:** scrape is a **PodMonitor** (not
  ServiceMonitor) in `catalog/ai-gateway` — port 1064 is a container
  port with no Service; monitor self-declares with the chart and
  selects across namespaces via `namespaceSelector`. First verify
  attempt raced the operator's config reload (~60–90s) — check
  `/api/v1/targets` scrapePools before diagnosing deeper. extproc
  counters reset on pod restart (normal; `rate()` handles it).
  Signal validated live: **23.97 output tok/s** on the exact KEDA
  query after a 1098-token completion.
- **envoyHpa demo finding:** EG created the HPA but CPU stayed
  `<unknown>` — the injected extproc/shutdown-manager containers carry
  no CPU requests (the known 2.5 gap), and HPA pod-CPU% needs requests
  on EVERY container. KEDA's external metric is immune. Fix the
  injected containers' resources at the hardening pass.
- **2026-07-25 — item 6, the scale event (README material):** k6 ran
  **in-cluster** (grafana/k6 Job + ConfigMap script, ephemeral —
  torn down after; recorded here rather than committed). The load
  generator got its **own gateway consumer** (`k6-loadtest`, SOPS'd,
  kept for future runs with its k8s Secret `k6-gateway-key`) — even
  test rigs follow per-consumer auth. Profile: 4 VUs × 4 min, long
  generations (max_tokens 700), 1–3s think time. **Results: 29
  requests, 100% success, avg 34.2s, p95 36.9s.** Scale arc (20s
  monitor + Prometheus range query): 1 replica baseline → desired 2
  at ~+40s (67.9 tok/s ⇒ 44.8 avg vs 30 target) → desired 3
  (ceiling) at ~+60s → 3/3 ready by ~+85s → plateau 45–84 tok/s
  (single-GPU Ollama is the throughput bound, exactly as predicted —
  the demo proves the CONTROL LOOP; capacity becomes real with cloud
  backends). Ceiling never exceeded.
- **Scale-down semantics lesson:** KEDA's `cooldownPeriod` only
  applies to scale-to-zero; with minReplicas ≥ 1 the down-scale is
  the HPA `scaleDown` stabilization window (default **300s**).
  Values comment corrected. Confirmed live: 3→2 at 16:45:43, →1 at
  16:45:59 — almost exactly 300s after the token rate zeroed.
- **SOPS append incident (recovered):** hand-appending a consumer to
  the decrypted YAML via `printf` broke indentation; `sops -e -i`
  refused, briefly leaving plaintext in the working tree
  (uncommitted). Recovery: `git checkout` of the last-good encrypted
  file, then rebuild via python-yaml with round-trip validation
  before encrypting. Rule: never hand-append structured secrets —
  build, validate, then encrypt (same family as the hosts-file
  lesson: validate before destructive writes).
- **KEDA 2.20.1** as `catalog/keda` umbrella. Handoff verified: with
  `mode: keda`, EG garbage-collected its HPA and only
  `keda-hpa-ai-gateway-tokens` remains (two HPAs on one Deployment
  would flap). ScaledObject READY=True, ACTIVE=False at idle, 1/1
  replicas. Threshold 30 output-tok/s per replica: one active qwen3.5
  stream ≈ 70 tok/s, so a busy demo pushes toward the 3-replica
  ceiling. Honest caveat: local Ollama is the real throughput ceiling
  (single GPU) — this demonstrates the control loop; it becomes real
  capacity with cloud backends (Phase 8).
