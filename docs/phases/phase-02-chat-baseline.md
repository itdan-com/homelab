# Phase 2 — Chat baseline as catalog-pattern Helm charts

**Goal:** Establish the `catalog/` directory convention; deploy OpenWebUI + LiteLLM + Postgres as the first three catalog entries; `git init` and push to GitHub.

**Status:** In progress. P2.1–P2.5 + P2.12 done; P2.6–P2.11 remaining.

---

## High-level outline

1. Decide the `catalog/` layout and the self-declaring label schema (`needs-sso`, `llm-traffic`, `wants-vector`, `exposes-mcp`) — these labels are what later phases automate against.
2. Write `catalog/postgres/` Helm chart (the dependency for the other two).
3. Write `catalog/litellm/` Helm chart; configured to reach Ollama on the host via `host.docker.internal:11434` and optionally a cloud LLM API key.
4. Write `catalog/openwebui/` Helm chart; point it at LiteLLM.
5. `helm install` each into a `chat` namespace; verify OpenWebUI reachable in browser; verify a prompt round-trips through LiteLLM to Ollama.
6. `git init` in `~/homelab`; first commit; create GitHub repo and push.

## Granular checklist

Tracked across sessions. Tick as you go.

### Groundwork (done in prior session, 2026-05-17)

- [x] **P2.1** Verify Ollama path end-to-end: pod → `host.docker.internal:11434` → host Ollama returns a generation. Fixed Windows-side binding via `setx OLLAMA_HOST "0.0.0.0:11434"` + tray-relaunch.
- [x] **P2.2** `git init` in `~/homelab`; first commit landing CLAUDE.md + STATUS.md + phase docs + operator cheatsheet.
- [x] **P2.3** Declarative cluster config at `k3d/devlab-cluster.yaml` (replaces the imperative `k3d cluster create` from Phase 1).
- [x] **P2.4** Rebuild cluster from the YAML with per-node memory caps; scheduler-visible RAM dropped 124 GiB → 28.7 GiB (the win — prevents overcommit catastrophes once we have real workloads).
- [x] **P2.5** Reattach Portainer to the rebuilt cluster (Portainer Agent re-deployed via `kubectl apply`).
- [x] **P2.12** Push to private GitHub: `itdan-com/homelab`. Three commits on `main`.

### Chart-writing marathon (next batch)

- [x] **P2.6** Write `catalog/README.md`: directory convention, label schema, secrets strategy, ingress decision. **This is the contract every later chart and every later phase keys off** — design before typing. Sub-items:
  - [x] Confirm label set: six labels — `needs-sso`, `llm-traffic`, `wants-vector`, `exposes-mcp` (chart-level annotations) + `tier`, `data-class` (release-level values). Trust-gradient design (sandbox auto-approves, dev requires tap, prod max friction) saved to memory.
  - [x] Decide label propagation pattern: chart-level in `Chart.yaml.annotations` AND release-level in `values.yaml`, both emitted as Kubernetes `labels:` on every rendered object via a shared `_helpers.tpl` (lives in `catalog/_template/`).
  - [x] Decide secrets approach for Phase 2 (pre-Sentinel): SOPS + age. `.sops.yaml` at repo root encrypts only VALUES (key names stay diff-readable). Phase 6 migration plan documented in README: bootstrap secrets stay in SOPS, external-system credentials graduate to Sentinel-issued short-lived tokens.
  - [x] Decide ingress approach: Traefik IngressRoute + `*.lab.local` hosts entries on the Mac. cert-manager + TLS bolts on in Phase 5 with no chart changes.
  - [x] Build `catalog/_template/` skeleton chart with `_helpers.tpl` carrying the label-propagation contract. Smoke-tested live on the cluster — labels propagate to Service/Deployment/Pod/IngressRoute, selectors return the right things, restricted PodSecurityStandard defaults work (using `nginxinc/nginx-unprivileged` as the placeholder image).
- [x] **P2.7** Write `catalog/postgres/` chart. **Approach pivoted from Bitnami subchart → hand-rolled mid-task** because Bitnami's `commonLabels` workaround would have been a wart on the catalog label contract; hand-rolled fits natively via `_helpers.tpl`. ~80 lines of YAML covering Service, Secret, PV, PVC, StatefulSet. SOPS-encrypted password (first real encrypted secret). Persistent storage via explicit PV with hostPath pointing into `/homelab-data` (mounted from `/home/bob/homelab-data` on the WSL2 host via `k3d/devlab-cluster.yaml`). **Verified end-to-end:** wrote two rows → deleted entire cluster → recreated → reinstalled chart → rows still present. CNPG migration to operator pattern planned for Phase 7.
- [x] **P2.8** Write `catalog/litellm/` chart from scratch. ConfigMap renders the model list from `values.yaml` (data-driven, not template-driven) so adding a model is a 1-line edit. Cloud-provider key slots (OpenAI/Anthropic/Gemini) pre-wired in `values.yaml`; activate via `secrets.enc.yaml`. SOPS-encrypted master key. Six catalog labels propagate; `llm-traffic: "true"` confirmed. **End-to-end verified:** chat-completion request to `qwen3.5` returned a real response from Ollama on the host RTX 4070. The Sentinel proxy injection point for Phase 5.5 now exists at exactly one place (the `llm-traffic: true` selector).
- [x] **P2.9** Wrote `catalog/openwebui/` chart. `OPENAI_API_BASE_URL` → in-cluster LiteLLM Service (`http://litellm:4000/v1`); chat-history PVC (hostPath under `/homelab-data`) at `/app/backend/data`; `needs-sso: true`. Image pinned to `v0.9.6`; `strategy: Recreate` (RWO PV can't rolling-update). **Pivoted to a scoped virtual key** (owner's least-privilege choice over the master key) — required wiring LiteLLM→Postgres; see notes for the non-root/prisma saga and the hardened-root resolution.
- [x] **P2.10** `chat` namespace pre-existed; `helm secrets install` all three. `helm list -n chat` shows postgres + litellm + openwebui all `deployed`, pods `Running`, no crash loops. **In-cluster e2e:** `PONG` round-trips from OpenWebUI's own pod → LiteLLM (virtual key) → Ollama.
- [ ] **P2.11** End-to-end browser test: OpenWebUI reachable from Mac; send a prompt; verify it round-trips through LiteLLM to host Ollama. Commit the working catalog. Update STATUS to mark Phase 2 done.

## Open questions to resolve at the start

- Use Bitnami charts as a base, or write from scratch for learning value? (Recommendation: scratch for openwebui/litellm since they're the heart of the lab; Bitnami for Postgres which is a commodity.)
- Where does the LiteLLM master key live in Phase 2 (pre-Sentinel)? Likely a Kubernetes Secret with a TODO to migrate to Sentinel-issued ephemeral tokens in Phase 6.
- Ingress: NodePort for now (simple), or Traefik (k3d's default) with `/etc/hosts` entries?

## Phase exit criteria

- `helm list -A` shows the three catalog services healthy.
- OpenWebUI accessible at `http://<host>:<port>` from the Mac browser.
- A test prompt sent through OpenWebUI returns a response from Ollama via LiteLLM.
- `~/homelab` is a git repo pushed to GitHub.
- `STATUS.md` updated.

## Notes captured during execution

- 2026-05-17 — k3d memory suffix is `g` not `Gi` (cluster create silently rejects `Gi`).
- 2026-05-17 — CoreDNS needs a post-create pod-delete to honor `hostAliases`; there's a race with the upstream cache otherwise.
- 2026-06-02 — `kubectl get ingressroute` defaults to the OLD `traefik.containo.us/v1alpha1` API group, which is empty in current k3d. Charts use the current `traefik.io/v1alpha1` group. Use `kubectl get ingressroutes.traefik.io` explicitly to avoid the misleading "No resources found" answer.
- 2026-06-02 — The standard nginx image cannot run under restricted PodSecurityStandard defaults (`runAsNonRoot: true`) because it binds port 80 as root. The `_template` chart ships with `nginxinc/nginx-unprivileged` (binds 8080) as a placeholder that actually runs under its own security policy. Real charts: pick images that respect non-root by default.
- 2026-06-02 — Initial values.yaml had `image.tag: ""` with `appVersion: "0.1.0"` — caused `ErrImagePull` because there's no `nginx:0.1.0`. The Helm convention is "appVersion IS the default image tag" — set appVersion to a real upstream version, not the chart's semver.
- 2026-06-02 — k3s hostPath PVs do NOT honor `fsGroup` automatically (kubelet doesn't recurse-chown arbitrary host paths — too dangerous on shared volumes). For the Postgres chart, the host directory at `/home/bob/homelab-data/postgres-<release>` must be `chown -R 999:999` BEFORE first install. Documented inline in `catalog/postgres/values.yaml`. CNPG (planned Phase 7) handles this internally — a real reason to graduate to the operator pattern later.
- 2026-06-02 — The default `_template/templates/service.yaml` hardcodes port name `http`, which doesn't generalize to non-HTTP services (e.g. Postgres needs `postgres`). For now we edit per-chart; future task to parameterize via a values key. Filed against `_template` as future cleanup.
- 2026-06-02 — LiteLLM OOMKills at 1Gi memory limit even with one model — the Python proxy loads ~1Gi of connectors at startup regardless of how many are actually used. Floor is 2Gi limit / 512Mi request. Documented inline in `catalog/litellm/values.yaml`. The cost-tracking dashboard in Phase 7 will key off `llm-traffic: "true"` so this label propagation is the bridge that makes Phase 7 work without per-chart wiring.
- 2026-06-03 — **LiteLLM DB mode (virtual keys) is incompatible with a non-root securityContext on this image.** LiteLLM bakes prisma's query-engine binary into root-only `/root/.cache` at build time; a non-root UID can't read it, re-fetches an incomplete engine set, and runtime queries fail with `Not connected to the query engine` (migrations still succeed — they use a different engine, which masked the problem). Verified via a debug pod that root mode works fine. Resolution: litellm + openwebui run **hardened-root** (`runAsUser: 0` but `drop: [ALL]` caps, `allowPrivilegeEscalation: false`, seccomp RuntimeDefault). For a gateway, the load-bearing least-privilege controls are the scoped virtual key + NetworkPolicy + SSO, not the container UID.
- 2026-06-03 — Sequencing gotcha: a root initContainer that runs `chown` needs `CAP_CHOWN` added back — `capabilities.drop: [ALL]` removes it *even for root*, so `chown` fails with "Operation not permitted." (Moot in the final design since we went hardened-root, but a real lesson.)
- 2026-06-03 — `qwen3.5` is a reasoning model: it spends ~200 tokens "thinking" before any visible output, so a stingy `max_tokens` (or Ollama's default `num_predict`) yields an empty `content`. Give clients generous token budgets.
