# Phase 2.5 — Gateway swap: LiteLLM → Envoy AI Gateway

**Goal:** Replace LiteLLM with a production-grade, Kubernetes-native
AI gateway: OpenWebUI talks OpenAI-compatible API to the gateway, the
gateway routes to host Ollama (later: cloud LLMs), consumers hold
scoped per-client keys, and token/request metrics are scrapeable —
ready to be Phase 3's KEDA signal. Primary: **Envoy AI Gateway**.
Fallback: **Bifrost**. See ADR-001 for the full rationale.

**Status:** Not started. Track 0 repair (CoreDNS fix) done 2026-07-25.

**Timebox rule:** if the Envoy spike (2.5.1–2.5.2) is not serving a
completion by the end of its first session, switch to the Bifrost
path (2.5.B) rather than grinding — record why in Notes. Either way
the chart is `catalog/ai-gateway/` — the implementation must stay
swappable behind the catalog contract.

---

## Checklist

### 2.5.1 Spike: control plane up

- [ ] Read the CURRENT Envoy AI Gateway quickstart — it is pre-1.0
      and CRD shapes drift; do not trust memory. Record the exact
      chart + app versions in Notes.
- [ ] Helm-install Envoy Gateway + the AI Gateway extension into
      `envoy-gateway-system`, explicit resource requests/limits.
- [ ] GatewayClass + Gateway accepted, controller pods Ready.
- [ ] **Success criterion:** `kubectl get gateway` shows Programmed.

### 2.5.2 Route to Ollama

- [ ] Backend + AIServiceBackend for host Ollama
      (`host.docker.internal:11434`, OpenAI-compatible `/v1` schema).
- [ ] AIGatewayRoute exposing `qwen3.5` on the unified endpoint.
- [ ] **Success criterion:** in-cluster curl to the gateway returns a
      real `qwen3.5` completion (generous `max_tokens` — reasoning
      model burns ~200 think-tokens; see Phase 2 notes).

### 2.5.3 Keys, catalog packaging, cutover

- [ ] Per-consumer API-key auth: OpenWebUI gets its own key,
      SOPS-encrypted — keeps the scoped-key least-privilege property
      from P2.9. No shared master key for clients.
- [ ] Wrap the CRs in `catalog/ai-gateway/`: all six catalog labels
      propagate; `llm-traffic: "true"` (this selector is the Phase
      5.5 Sentinel injection point). Model list rendered from
      `values.yaml` (data-driven, like litellm's was).
- [ ] Point OpenWebUI `OPENAI_API_BASE_URL` + key at the gateway;
      rollout; in-cluster e2e PONG through the new path.
- [ ] Prometheus metrics endpoint confirmed: curl shows token/request
      series (name the exact metrics in Notes — Phase 3 keys off them).
- [ ] **Success criterion:** browser chat works end-to-end via the
      new gateway.

### 2.5.4 Decommission LiteLLM

- [ ] `helm uninstall litellm -n chat`; delete `catalog/litellm/`.
- [ ] Drop `litellm` from postgres `auth.extraDatabases` (only
      virtual-key state lived there — disposable). Decide the unused
      `openwebui` DB while in there (backlog item: repurpose via
      `DATABASE_URL` or drop).
- [ ] Sweep references → `ai-gateway`: operator cheatsheet, `/resume`
      liveness-gate step 4 deploy name, STATUS backlog mentions.
- [ ] Update memory `litellm-openwebui-hardened-root.md` — litellm
      half is obsolete; note whether the new gateway runs non-root
      (Envoy images do).

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

- (empty)
