# Phase 2 — Chat baseline as catalog-pattern Helm charts

**Goal:** Establish the `catalog/` directory convention; deploy OpenWebUI + LiteLLM + Postgres as the first three catalog entries; `git init` and push to GitHub.

**Status:** Not started. Blocked on Phase 1.

---

## High-level outline

1. Decide the `catalog/` layout and the self-declaring label schema (`needs-sso`, `llm-traffic`, `wants-vector`, `exposes-mcp`) — these labels are what later phases automate against.
2. Write `catalog/postgres/` Helm chart (the dependency for the other two).
3. Write `catalog/litellm/` Helm chart; configured to reach Ollama on the host via `host.docker.internal:11434` and optionally a cloud LLM API key.
4. Write `catalog/openwebui/` Helm chart; point it at LiteLLM.
5. `helm install` each into a `chat` namespace; verify OpenWebUI reachable in browser; verify a prompt round-trips through LiteLLM to Ollama.
6. `git init` in `~/homelab`; first commit; create GitHub repo and push.

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

- (empty)
