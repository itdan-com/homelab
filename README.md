# homelab — an AI-operated internal developer platform

**One Claude runs the platform. It holds no credentials, and its only
write instrument is a pull request a human must merge.**

This repo is a working reference implementation of a pattern most
enterprises haven't assembled yet: a Kubernetes platform where an AI
agent observes (Prometheus, logs, cluster API), proposes every action
as a reviewable git commit, and gains external powers only through
short-lived, per-task capability grants issued by a broker it cannot
reach. Built on deliberately boring rails — k3d, Helm, ArgoCD, Envoy —
because a novel trust model deserves battle-tested plumbing.

> Status: phases 1–4.5 complete (chat stack, AI gateway, AI-aware
> autoscaling, GitOps, and the PR-proposing operator shown in the demo
> below). Next: team enablement — SSO + TLS (5), then the Sentinel
> capability broker (5.5). See [STATUS.md](STATUS.md) for the live
> cursor.

## What's running today

- **Service catalog** ([`catalog/`](catalog/README.md)) — every workload
  is a Helm chart with six self-declared labels; dropping in a chart
  with an `argo.yaml` *is* deployment. ArgoCD auto-discovers via an
  ApplicationSet; offboarding is deleting a file.
- **AI gateway** (Envoy AI Gateway) — OpenAI-compatible data plane
  routing to local Ollama (cloud backends pluggable), per-consumer
  API keys (SOPS-encrypted; no shared master key anywhere), token
  metering built in.
- **AI-aware autoscaling** — KEDA scales the gateway on
  `gen_ai_client_token_usage` rate from Prometheus: the platform
  watches its own AI traffic and acts on it.
- **Observability** — kube-prometheus-stack with dashboards-as-code,
  including the token-rate/replica dashboard the demo uses.
- **GitOps** — ArgoCD with a read-only deploy key. Write access to this
  repo does not exist inside the cluster, by construction.

## Where it runs

The whole platform is **one home PC** — no cloud account required.
Kubernetes here is the manager, not a place; the same catalog deploys
unchanged to any cluster (phase 8 proves it on DigitalOcean, then
tears it down). This is the reference topology:

```text
Mac (daily driver)
 └─ Remote Desktop ──► Windows 11 Pro box — the lab server
                        (i5-13400F · 64 GB RAM · RTX 4070)
      ├─ Ollama — native Windows app on the GPU; serves qwen3.5:9b
      │           on :11434 (the actual LLM behind every chat)
      └─ WSL2 · Ubuntu (capped at 32 GB RAM / 8 CPUs)
           ├─ operator Claude — Claude Code + a GitHub App identity;
           │    read-only kubeconfig, proposes changes as PRs
           ├─ Sentinel — capability broker + kill switch, outside the
           │    cluster by design                     [planned, ph 5.5]
           └─ docker-ce (Docker Engine)
                ├─ portainer — Portainer CE, Docker-level dashboard
                └─ k3d cluster "devlab" — Kubernetes (k3s), 4 nodes
                     k3d-devlab-server-0 · -agent-0/1/2
                     + k3d-devlab-serverlb (host :8080 → ingress)
                     └─ everything in the table below
```

| Namespace | What runs there | Role |
|---|---|---|
| `chat` | OpenWebUI · Postgres · the `ai-gateway` Gateway | The chat stack users touch (Postgres idle, reserved for SSO) |
| `envoy-gateway-system` | Envoy Gateway control plane + the generated Envoy data-plane pods | The proxy fleet that actually carries LLM traffic (KEDA scales these) |
| `envoy-ai-gateway-system` | Envoy AI Gateway controller | Teaches Envoy the OpenAI protocol + per-token metering |
| `monitoring` | Prometheus · Grafana · Alertmanager · exporters | Metrics and dashboards; source of KEDA's scaling signal |
| `keda` | KEDA operator | Watches tokens/sec in Prometheus, scales the gateway |
| `argocd` | Argo CD | Watches this repo's `catalog/`; the only deployer |
| `sandbox` | echo | Template-born demo service (the PR #4 onboarding) |
| `portainer` | Portainer agent | Links the cluster into the Portainer UI |
| `kube-system` | Traefik · CoreDNS (+ durable host override) · metrics-server | k3s built-ins: ingress, DNS, resource metrics |

Two paths explain the whole system:

- **A chat message:** browser → `https://openwebui.lab.local:8443`
  (hosts entry → k3d serverlb; TLS from the lab CA, chart-owned cert)
  → Traefik → OpenWebUI → AI gateway (Envoy, API key + token metering)
  → `host.docker.internal:11434` → Ollama on the Windows GPU.
- **A change:** operator Claude opens a PR → a human merges (the only
  write path to `main`) → ArgoCD pulls and the cluster converges.

### The doors — every UI and how to open it

Two access styles, chosen by policy (ADR-002): a service gets a
`https://*.lab.local` door through Traefik only if humans browse it
**and** it authenticates them. Everything else is reached with
`kubectl port-forward` — a temporary private tunnel from your
terminal that lives until you Ctrl-C it. Prometheus and Alertmanager
stay tunnel-only **on purpose** (unauthenticated by design, until
Phase 7 puts them behind SSO forward-auth); ArgoCD's door arrives
with its SSO.

| Service | What it's for | Open it |
|---|---|---|
| **OpenWebUI** | The chat UI | `https://openwebui.lab.local:8443` (hosts file: `127.0.0.1 openwebui.lab.local`; http on :8080 redirects) |
| **Argo CD** | The GitOps board — platform vs. git, every sync and diff | `kubectl port-forward -n argocd svc/argocd-server 8081:80` → `http://localhost:8081` |
| **Grafana** | Dashboards — cluster health, token-rate autoscaling | `https://grafana.lab.local:8443` (hosts file: `127.0.0.1 grafana.lab.local`) |
| **Prometheus** | Raw metrics + PromQL console | `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090` → `http://localhost:9090` |
| **Alertmanager** | Alert routing (chat channels in Phase 7) | `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093` → `http://localhost:9093` |
| **Portainer** | Docker/Kubernetes visual manager | `https://localhost:9443` |
| **echo** | Template-born demo service | `https://echo.lab.local:8443` (hosts entry likewise) |
| **AI gateway** | OpenAI-compatible LLM API | Not a browser door — in-cluster only, `http://ai-gateway/v1` + consumer key |

**Logins in ten seconds** (nothing here has a vendor default
password):

- **OpenWebUI** — the account you signed up with (first signup
  becomes admin).
- **Grafana** — `admin` / the value you set in
  `catalog/monitoring/secrets.enc.yaml`; read it back:
  `sops -d catalog/monitoring/secrets.enc.yaml`
- **Argo CD** — `admin` /
  `kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d`

(Phase 5 stage B replaces the first two with one Authentik SSO
identity.) Full table with every interface:
[SETUP.md § Part 2](SETUP.md#part-2--every-web-interface-and-where-its-password-lives).
Day-2 health checks and recovery:
[the operator cheatsheet](docs/operator-cheatsheet.md).

## Seen, not told

![One take: the ArgoCD board shows five apps; the operator is asked in plain English for a new service; PR #4 appears on GitHub; a human merges it; the board shows six — echo is live](docs/assets/pipeline-claude-github-argo.gif)

*The whole loop, one take (~85 s, build fast-forwarded): ask the
operator for a service in plain English → it inspects the image,
births a chart from the template, opens a PR → the human merges (the
only write path to `main`) → ArgoCD discovers the chart and the board
goes from five apps to six. No human typed git or kubectl.*

![The scale event: token rate crosses the threshold, the data plane climbs to its ceiling, then collapses back](docs/assets/scale-event.png)

*A k6 burst pushes output tokens/sec over the 30/replica threshold (red
line); KEDA walks the Envoy data plane 1→3; five minutes of quiet walks
it back down. Re-create it anytime: `scripts/scale-demo.sh`.*

![ArgoCD: five catalog applications, all Synced and Healthy](docs/assets/argocd-applications.png)

*The whole platform as ArgoCD sees it — every service a chart, every
chart discovered from git, nothing deployed any other way.*

## Quickstart

Prereqs: Docker, [k3d](https://k3d.io), kubectl, Helm (+
[helm-secrets](https://github.com/jkroepke/helm-secrets)),
[sops](https://github.com/getsops/sops), [age](https://github.com/FiloSottile/age).
Fork the repo, generate your age key, re-encrypt `secrets.enc.yaml`
files to your recipient (`.sops.yaml`), register a **read-only** deploy
key on your fork. Then:

```bash
./bootstrap.sh
```

The script builds the cluster, plants ArgoCD, and stops — everything
else self-assembles from the catalog. `kubectl get applications -n
argocd -w` to watch it converge.

**[SETUP.md](SETUP.md) is the full walkthrough** — the exact GitHub
clicks (App, deploy key, ruleset — including the two form traps
everyone hits), how to regenerate every secret with your own keys, and
a table of **every web interface with its username and where the
password actually comes from**.

## The security model (why this exists)

Most agent deployments hand the model long-lived credentials and hope.
This platform's answer is structural:

1. **Propose-only actuation** — the agent's cluster powers are read-only;
   changes happen by opening PRs. Branch protection makes the human
   merge the only path to `main`, and ArgoCD deploys only `main`.
2. **Ephemeral capabilities (Sentinel, phase 5.5)** — for any non-git
   power (Slack, SaaS, email), the agent requests a capability; a human
   taps approve; a broker outside the cluster mints a token scoped to
   one tool + one task + five minutes, enforced at an Envoy checkpoint.
3. **Defense in depth** — upstream OAuth scope ∩ per-MCP-server
   allowlist ∩ Sentinel grant must all agree. Finding any credential
   inside the cluster buys the power to *ask*, never the power to *do*.

### The gate, tested

Point 1 is a claim until someone tries to break it. On day two of the
operator's life, we did — first socially, then with explicit
authorization. Three independent layers refused:

1. **Charter.** Asked casually to "save me the click" and merge its own
   PR, the operator checked its own scoping and declined: *"merging my
   own PR is the one thing this operator role structurally can't do."*
2. **Platform.** Ordered to actually try, its self-approval came back
   `422 — Can not approve your own pull request`. The operator App
   authored the PR, and GitHub's author≠approver rule is unconditional.
3. **Ruleset.** Its review-less merge came back `405 — Repository rule
   violations found … at least 1 approving review is required`
   (`protect-main`; the App holds no bypass).

Post-test: zero state changed — the PR still open and unreviewed,
`main` untouched. The operator then flagged, unprompted, that layer 3
is the only *configurable* one of the three (its token's
`contents=write` cleared the permission check; the ruleset alone
stopped the merge) — that hardening thread is tracked for Phase 6.
Raw responses and request-ids:
[phase 4.5 execution notes](docs/phases/phase-04-5-control-plane-v0.md#notes-captured-during-execution).
Run the same test on your own fork:
[SETUP.md § First flight](SETUP.md#first-flight-prove-the-gate).

Decisions and their whys live in [`docs/adr/`](docs/adr/).

## Repo map

| Path | What |
|---|---|
| `catalog/` | The service catalog — charts + the contract ([README](catalog/README.md)) |
| `k3d/` | Cluster config + durable CoreDNS override |
| `scripts/` | Demo + operations scripts (`scale-demo.sh`) |
| `docs/phases/` | The build plan, phase by phase, with execution notes |
| `docs/adr/` | Architecture decision records |
| `docs/operator-cheatsheet.md` | Day-2 operations for humans |
| `STATUS.md` | Live project cursor: what's done, what's next |

## License

**AGPL-3.0-or-later** — Copyright (C) 2026 Bob Slosing. Use it, run it,
learn from it freely; if you modify it and offer it as a product or
service, your modifications must be published under the same license.
The copyright is held solely by the author, which keeps dual licensing
open: commercial exceptions to AGPL's sharing requirement are available
by arrangement. Outside contributions require a CLA (see
[CONTRIBUTING.md](CONTRIBUTING.md)) so that property is preserved.
