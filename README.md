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

> Status: phases 1–4 complete (chat stack, AI gateway, AI-aware
> autoscaling, GitOps). The PR-proposing operator (4.5) and the
> Sentinel capability broker (5.5) are the next milestones. See
> [STATUS.md](STATUS.md) for the live cursor.

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

## Seen, not told

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
