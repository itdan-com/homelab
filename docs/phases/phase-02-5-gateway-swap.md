# Phase 2.5 — Gateway swap: LiteLLM → Envoy AI Gateway

**Goal:** Replace LiteLLM with a production-grade, Kubernetes-native
AI gateway: OpenWebUI talks OpenAI-compatible API to the gateway, the
gateway routes to host Ollama (later: cloud LLMs), consumers hold
scoped per-client keys, and token/request metrics are scrapeable —
ready to be Phase 3's KEDA signal. Primary: **Envoy AI Gateway**.
Fallback: **Bifrost**. See ADR-001 for the full rationale.

**Status:** **PHASE COMPLETE 2026-07-25** — one session, Envoy path, Bifrost fallback unused (AI Gateway hit v1.0.0 GA; the pre-1.0 risk ADR-001 hedged against no longer exists). All exit criteria met: browser chat via the gateway, scoped SOPS consumer key, `gen_ai_*` metrics scrapeable, `catalog/litellm/` gone, six labels on `catalog/ai-gateway/`.

**Timebox rule:** if the Envoy spike (2.5.1–2.5.2) is not serving a
completion by the end of its first session, switch to the Bifrost
path (2.5.B) rather than grinding — record why in Notes. Either way
the chart is `catalog/ai-gateway/` — the implementation must stay
swappable behind the catalog contract.

---

## Checklist

### 2.5.1 Spike: control plane up

- [x] Read the CURRENT Envoy AI Gateway quickstart — it is pre-1.0
      and CRD shapes drift; do not trust memory. Record the exact
      chart + app versions in Notes. *(v1.0.0 GA — see Notes)*
- [x] Helm-install Envoy Gateway + the AI Gateway extension into
      `envoy-gateway-system`, explicit resource requests/limits.
      *(AI controller in upstream-default `envoy-ai-gateway-system` — see Notes)*
- [x] GatewayClass + Gateway accepted, controller pods Ready.
- [x] **Success criterion:** `kubectl get gateway` shows Programmed.
      *(Programmed=True 12s after apply)*

### 2.5.2 Route to Ollama

- [x] Backend + AIServiceBackend for host Ollama
      (`host.docker.internal:11434`, OpenAI-compatible `/v1` schema).
- [x] AIGatewayRoute exposing `qwen3.5` on the unified endpoint.
- [x] **Success criterion:** in-cluster curl to the gateway returns a
      real `qwen3.5` completion (generous `max_tokens` — reasoning
      model burns ~200 think-tokens; see Phase 2 notes).
      *(PASS: "Kubernetes is an open-source platform…", finish=stop,
      909 tokens @ ~70 tok/s. Think-burn is worse than ~200 — see Notes.)*

### 2.5.3 Keys, catalog packaging, cutover

- [x] Per-consumer API-key auth: OpenWebUI gets its own key,
      SOPS-encrypted — keeps the scoped-key least-privilege property
      from P2.9. No shared master key for clients.
      *(EG SecurityPolicy apiKeyAuth on the Gateway; auth matrix
      verified: valid key 200 / no key 401 / wrong key 401.)*
- [x] Wrap the CRs in `catalog/ai-gateway/`: all six catalog labels
      propagate; `llm-traffic: "true"` (this selector is the Phase
      5.5 Sentinel injection point). Model list rendered from
      `values.yaml` (data-driven, like litellm's was).
      *(10 objects rendered, 10/10 carry all six labels.)*
- [x] Point OpenWebUI `OPENAI_API_BASE_URL` + key at the gateway;
      rollout; in-cluster e2e PONG through the new path.
      *(env `http://ai-gateway/v1` + scoped key confirmed in pod;
      e2e 200 with real completion via the stable name. Values key
      renamed litellm.apiKey → gateway.apiKey across the chart.)*
- [x] Prometheus metrics endpoint confirmed: curl shows token/request
      series (name the exact metrics in Notes — Phase 3 keys off them).
      *(Verified from in-cluster curl before cutover; details in Notes.)*
- [x] **Success criterion:** browser chat works end-to-end via the
      new gateway. *(Owner confirmed 2026-07-25: logged into
      openwebui.lab.local:8080, qwen3.5 replied — the only path is
      the gateway, so the whole chain is proven.)*

### 2.5.4 Decommission LiteLLM

- [x] `helm uninstall litellm -n chat`; delete `catalog/litellm/`.
- [x] Drop `litellm` from postgres `auth.extraDatabases` (only
      virtual-key state lived there — disposable). Decide the unused
      `openwebui` DB while in there (backlog item: repurpose via
      `DATABASE_URL` or drop).
      *(Both DBs DROPped live + removed from values; `database:
      postgres`, `extraDatabases: []`. Postgres stays as the
      relational store for Langfuse/Phase 5+.)*
- [x] Sweep references → `ai-gateway`: operator cheatsheet, `/resume`
      liveness-gate step 4 deploy name, STATUS backlog mentions.
      *(Also: catalog/README examples, openwebui Chart.yaml/NOTES,
      phase-05/07 mentions. Historical records — ADR-001, activity
      logs, phase-02 — left as written.)*
- [x] Update memory `litellm-openwebui-hardened-root.md` — litellm
      half is obsolete; note whether the new gateway runs non-root
      (Envoy images do). *(Done — envoy/extproc run non-root.)*

### 2.5.B Fallback path: Bifrost (only if the timebox trips)

- [ ] Same shape, different engine: `catalog/ai-gateway/` chart
      around the Bifrost single binary; identical key/label/metrics/
      cutover items as 2.5.3–2.5.4.

## Open questions to resolve at the start

- Exact CRD names/shapes at the installed version (pre-1.0 churn).
- Streaming behavior through the gateway to Ollama; qwen3.5 `<think>`
  block handling in OpenWebUI UX (existing backlog note).
- Per-key token budgets / rate limits now, or defer to Phase 3?

## Phase exit criteria

- Browser chat round-trips via the new gateway (Envoy, or Bifrost if
  the fallback fired).
- Scoped per-consumer key SOPS-encrypted; clients hold no master key.
- Token/request metrics scrapeable by Prometheus.
- `catalog/litellm/` gone; six labels intact on `catalog/ai-gateway/`.
- `STATUS.md` updated.

## Notes captured during execution

- **2026-07-25 — versions (2.5.1):** Envoy AI Gateway **v1.0.0 GA**
  (stable `v1beta1` CRDs incl. AIGatewayRoute/AIServiceBackend/
  BackendSecurityPolicy — and `MCPRoute`, interesting for Phase 6).
  Envoy Gateway **v1.8.1**, Gateway API v1.5.1 bundled. Charts (all
  OCI, docker.io/envoyproxy): `gateway-helm@v1.8.1`,
  `ai-gateway-crds-helm@v1.0.0`, `ai-gateway-helm@v1.0.0`.
- **k8s version floor:** docs say 1.32+; ran clean on k3s v1.31.5.
  Re-check on any future failure before blaming config.
- **Deviation from this doc:** AI controller lives in upstream-default
  `envoy-ai-gateway-system` (the pinned `envoy-gateway-values.yaml`
  extension-manager FQDN expects it), not `envoy-gateway-system`.
- **Install mechanics:** the "enable the extension" step IS the
  `-f manifests/envoy-gateway-values.yaml` (pinned to the v1.0.0 tag,
  never `main`) on the EG install — it wires EG's extensionManager to
  `ai-gateway-controller…:1063` and sets `enableBackend: true`. The
  AI controller then injects an ext-proc sidecar into the Envoy pod
  (3-container data plane: envoy + shutdown-manager + extproc).
- **Data plane choices:** Gateway lives in `chat`; EnvoyProxy CR sets
  `envoyService.type: ClusterIP` (a LoadBalancer would make k3s svclb
  fight Traefik for host port 80) + explicit small resources.
  ClientTrafficPolicy raises Envoy's 32KiB client buffer to 50Mi (LLM
  bodies). Gap to close at hardening: the injected extproc container's
  resources are upstream defaults, not ours.
- **Token metering confirmed day one:** `usage` {prompt/completion/
  total} returned through the gateway — Phase 3's KEDA signal exists.
- **qwen3.5 think behavior through Ollama `/v1`:** reasoning arrives
  in a separate `message.reasoning` field (content stays clean).
  Constraint-style canaries ("reply with exactly one word: PONG")
  trigger 2000+-token think loops → `finish_reason: length`, empty
  content; `/no_think` soft switch is ignored by qwen3.5:9b. Canary
  prompts must be plain short questions with `max_tokens ≥ 1500`.
- **Metrics endpoint (Phase 3 KEDA signal):** the `ai-gateway-extproc`
  container in the Envoy pod, port **1064** (`aigw-admin`), path
  `/metrics`. Series (OTel GenAI semconv, Prometheus-rendered):
  `gen_ai_client_token_usage_{bucket,sum,count}` with labels
  `gen_ai_request_model`, `gen_ai_token_type` (input/output/total),
  `gen_ai_operation_name`, `gen_ai_provider_name`; plus request-
  duration/TTFT series in the same scrape. Envoy's own stats are
  separate on `:19001/stats/prometheus`. Candidate KEDA query:
  `rate(gen_ai_client_token_usage_sum{gen_ai_token_type="output"}[1m])`.
- **Cross-namespace Service lesson (cutover bug, fixed):** a Service
  selector only matches pods in its OWN namespace — the first "stable
  name" attempt selected EG's proxy pods across namespaces and got
  `connection refused` (Service resolved, zero endpoints). Fix: the
  chart's Service is now `type: ExternalName` aliasing the EG-generated
  Service, whose name is deterministic:
  `envoy-<ns>-<gw>-<sha256("<ns>/<gw>")[:8]>` — the chart computes the
  hash in-template (verified: `chat/ai-gateway` → `8e541394`), so
  nothing is hardcoded and consumers use `http://ai-gateway/v1`.
- **helm NOTES.txt renders too:** removing a values key (litellm →
  gateway rename) nil-pointered openwebui's NOTES.txt at render time.
  Sweep NOTES/comments when renaming values keys, not just templates.
- **Spike manifests** live in session scratchpad
  (`ai-gateway-spike.yaml`, `eg-values-v1.0.0.yaml`, `eg-resources.yaml`)
  — to be templated into `catalog/ai-gateway/` in 2.5.3.
