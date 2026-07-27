# Homelab Platform Engineering Project

## What this is

A homelab to build, scale, and operate an **AI-native internal developer
platform** on Kubernetes — designed as hands-on training for platform /
DevOps / SRE engineering skills, *and* as a working proof of a pattern
most enterprises haven't combined yet: **one Claude orchestrating a
multi-service AI platform under a ChatOps+GitOps approval gate, with
cross-stack control (K8s + external SaaS) via MCP**. The owner uses a
Mac day-to-day and operates a dedicated Windows 11 Pro box (via Remote
Desktop) as the lab server. The end product is also meant to be
**packaged and open-sourced** as a reference implementation (revenue
an optional upside) — build with adopters in mind: parameterized
paths, decisions recorded in `docs/adr/`, a demo asset per milestone.

This is a **learning exercise**. Explain concepts as you implement them —
do not just produce files silently. The owner wants to understand
Kubernetes, autoscaling, GitOps, infrastructure-as-code, and observability
well enough to be employable as a platform engineer. The owner is new to
git: they want only enough literacy to *read* diffs in Slack approval
messages — they will not type git commands. The control-plane bot
handles all git writes.

The end-state platform has these load-bearing properties:

- **Pluggable service catalog.** Every workload (OpenWebUI, the AI
  gateway, a prompt/skill gallery, a vector DB, external SaaS adapters, anything
  the owner wants to add later — Dust, other LLMs, etc.) lives as a Helm
  chart in `catalog/`. Adding a new service is dropping in a new chart;
  no bespoke wiring.
- **One control-plane Claude.** A long-running Claude Agent SDK
  workload in the `platform-control` namespace observes the cluster and
  proposes scaling/onboarding/remediation actions.
- **Hybrid ChatOps + GitOps approval.** Every action Claude proposes is
  auto-committed as a PR to the GitOps repo and posted to Slack with
  ✅/❌ buttons. On ✅, the bot merges and ArgoCD applies. The owner
  never types git but the commit history is a complete audit trail.
- **Cross-stack via MCP.** Each external system (GitHub, Slack,
  eventually Supabase / Railway / Sendgrid / Linear / etc.) is exposed
  to the control-plane Claude through a Model Context Protocol server
  Deployment in cluster. Adding a new SaaS = adding a new MCP
  Deployment.
- **Trust-domain separation via Sentinel.** Claude does NOT hold
  long-lived credentials for any external system, and Claude cannot
  reach the kill switch. A separate service called **Sentinel** runs
  on the WSL2 host as a systemd unit, *outside* the k3d cluster in a
  different trust domain. Sentinel acts as a credential broker: Claude
  requests capabilities per flow, the human grants them from a simple
  GUI, Sentinel mints short-lived scope-locked tokens, and a Sentinel
  proxy enforces scope + TTL + flow-tag on every MCP call. The cluster
  has no kubectl path, NetworkPolicy, or service account that reaches
  Sentinel's admin API — strict one-way trust. Three layers of kill
  (per-flow, per-capability, global) all live outside the cluster.
- **Per-flow ephemeral capabilities.** Each Claude task gets a unique
  `flow-id`. Capabilities are bound to flow-ids and time-limited
  (default 5 min). Two flows running in parallel cannot share each
  other's grants; revoking one does not affect the others. Long-lived
  agent identity is replaced with short-lived per-task capability
  bindings.
- **Audit and alerting.** Every action streams to `#claude-audit`;
  anomalies (secret reuse, abnormal action rate, unexpected namespaces
  touched, kill-switch flips) page `#claude-alerts`. Sentinel's audit
  log on the WSL2 host is the canonical source of truth.

## Current status

The canonical live state lives in **`STATUS.md`** at the repo root —
current phase, last action, what's next, recent activity log, backlog,
and blocked items. **Read `STATUS.md` first in every new session.**
This section in `CLAUDE.md` only captures long-running facts that
rarely change.

- WSL2 Ubuntu runs on the Windows box with a from-scratch docker-ce
  install (Docker Desktop retired). The 4-node `devlab` k3d cluster
  has been live since 2026-05-17 — declarative shape in
  `k3d/devlab-cluster.yaml`; the platform on it self-assembles from
  `catalog/` via ArgoCD.

## How sessions work

**Kickoff:** type `/resume` at the start of every new Claude session
in this repo. It's a project-level slash command at
`.claude/commands/resume.md` that walks Claude through the protocol
below and waits for owner confirmation before executing anything.

This project is built across many Claude sessions over weeks. To keep
context lean, avoid forgetting decisions, and conserve tokens, every
session follows a strict protocol:

1. **Start** by reading, in order: this `CLAUDE.md` (architecture &
   principles) → `STATUS.md` (where we are now) → the active phase
   doc at `docs/phases/phase-NN-*.md` (what to do next).
2. **Work** by ticking checklist items in the active phase doc.
   Append surprises and out-of-scope observations to `STATUS.md`'s
   Backlog section as they come up.
3. **End** by updating `STATUS.md`'s `Active phase`, `Status`,
   `Next action`, `Last updated`, and prepending a line to the
   `Recent activity log`.
4. **Use Sonnet** for executing checklist items. Reserve **Opus** for
   architectural decisions (conversations that revisit this file).
   Use **Haiku** for trivial one-shot edits.
5. **Use subagents** (`Explore`, `Plan`) for any codebase search or
   sub-task planning that would otherwise pull more than ~3 files
   into main context. Their context is separate; yours stays lean.
6. **Do not edit `CLAUDE.md` mid-session** except to record an
   architecture-level decision. The prompt cache rewards stability —
   churning `CLAUDE.md` evicts the cached prefix. Prefer the active
   phase doc and `STATUS.md` for working notes.
7. **`TaskCreate` is within-session only.** The markdown files in
   `docs/phases/` are the cross-session source of truth. Never put
   progress state into `TaskCreate` that needs to survive a session.

`CLAUDE.md` is the map. `STATUS.md` is the cursor. Phase docs are
the territory. Memory files (`~/.claude/projects/.../memory/`) hold
durable lessons and preferences not specific to any single phase.

## Environment

- Host: Windows 11 Pro, Intel i5-13400F (10c/16t), 64 GB RAM, RTX 4070,
  ~2 TB free NVMe.
- WSL2 is capped via `.wslconfig` at 32 GB RAM / 8 processors — stay
  within that budget.
- Keep all project files in the Linux filesystem (`~/homelab`), never on
  `/mnt/c/...` — cross-mount access is slow.
- Container runtime is **docker-ce** (Docker Community Edition) installed
  natively in WSL2 — not Docker Desktop. systemd must be enabled in WSL
  (`[boot] systemd=true` in `/etc/wsl.conf`) so `dockerd` runs as a
  proper service via `systemctl`.
- Because docker-ce does not auto-wire `host.docker.internal` the way
  Docker Desktop did, containers and the k3d cluster must be created
  with `host-gateway` mapping (e.g. `--add-host=host.docker.internal:host-gateway`
  on `docker run`, and the equivalent k3d flag on cluster create) for
  in-container traffic to reach the Windows host. Caveat learned
  2026-07-25: k3s can rewrite CoreDNS's `NodeHosts` on restart and
  silently drop the k3d-injected entry — `k3d/coredns-custom.yaml`
  is the durable fix; apply it after every cluster create.
- Ollama runs on the Windows host (native app, uses the RTX 4070).
  In-cluster pods reach it at `host.docker.internal:11434` once the
  host-gateway mapping above is in place.
- A DigitalOcean account is available for the cloud phase. DO uses
  per-second billing — always destroy cloud resources when finished.

## Build plan (remaining work)

**Phase 1 — host setup, Docker layer, and git literacy primer.** Enable
systemd in WSL2 (`[boot] systemd=true` in `/etc/wsl.conf`, then
`wsl --shutdown` from PowerShell). Install **docker-ce** from the
official Docker apt repo and add the user to the `docker` group so the
daemon is reachable without sudo. Install `kubectl`, `helm`, and `k3d`.
Deploy **Portainer CE** as a single container backed by a named volume
(`docker volume create portainer_data`) — this is the Docker-level web
dashboard that replaces what Docker Desktop's GUI used to show.
Then create the `devlab` k3d cluster (1 server + 3 agents) with the
`host.docker.internal` host-gateway mapping wired in. Register the k3d
cluster inside Portainer as a Kubernetes environment. **Also**: walk
the owner through a 20–30 min read-only git primer (commits, diffs,
branches, what a PR is). They must be able to read a diff in a Slack
approval message but will never type git commands.

**Phase 2 — chat baseline as catalog-pattern Helm charts.** Establish
the `catalog/` directory convention: each service is its own Helm chart
with self-declared labels (`needs-sso`, `llm-traffic`, `wants-vector`,
`exposes-mcp`) that downstream automation will key off. First three
catalog entries: **OpenWebUI**, **LiteLLM** (gateway, pointed at host
Ollama and any cloud LLMs), **Postgres**. Get the chat stack reachable
in a browser. `git init` in `~/homelab`, push to GitHub. *(Done.
LiteLLM was subsequently judged not production-grade — see ADR-001 —
and is replaced in Phase 2.5.)*

**Phase 2.5 — gateway swap (ADR-001).** Replace LiteLLM with **Envoy
AI Gateway** (CNCF Envoy Gateway's AI extension): production data
plane, Gateway API CRDs (GitOps-native), token-aware rate limiting,
Prometheus token metrics (Phase 3's KEDA signal), and `ext_authz`
reuse as the Sentinel enforcement point in Phase 5.5. Pre-1.0 risk is
managed by a one-session timebox with **Bifrost** as fallback. Chart
lives at `catalog/ai-gateway/` — implementation stays swappable.
Scoped per-consumer keys (SOPS) preserve the least-privilege property.

**Phase 3 — AI-aware autoscaling, metrics-first.** k3s's bundled
`metrics-server` covers CPU. Install **kube-prometheus-stack** core
(pulled forward from Phase 7 — KEDA's scaler needs it). CPU-based HPA
on OpenWebUI as the baseline, then **KEDA** scaling the AI gateway on
an AI-relevant signal from Prometheus — tokens/sec or in-flight
requests — with hard replica ceilings for the WSL2 budget. Drive load
with a bursty `k6` profile and capture the scale event in Grafana.

**Phase 4 — GitOps with app-of-apps.** Install ArgoCD. Configure the
**app-of-apps** pattern so a single root Application points at the
`catalog/` directory and ArgoCD auto-discovers every service inside.
This is what makes adding a service later as simple as "drop a chart in
`catalog/`." Demonstrate a manual commit auto-syncing.

**Phase 4.5 — Control-Plane v0 (PR-only, ADR-001).** The goal's magic
moment pulled forward: a dedicated **operator Claude Code instance**
with read-only cluster access (`view` RBAC) and a fine-grained GitHub
token that can only open PRs on this repo. Scale / stop / start /
onboard / rollback all happen as PRs with plain-English summaries;
the owner's merge is the approval gate; ArgoCD applies. Zero non-git
credentials — which is why it may precede Sentinel under the amended
rule (see Phase 5.5). Deliverable: the working demo loop + GIF.

**Phase 5 — team enablement layer (slimmed, ADR-001).** Add to
`catalog/`: **Authentik** (SSO/OIDC — OpenWebUI and Grafana first,
Portainer later) and **cert-manager** + Traefik for TLS on
`*.lab.local`. **MinIO** and the prompt-gallery/LLM-observability
service (Langfuse or similar) deploy on demand when something needs
them rather than by default. After this phase a friend or teammate
could be given an SSO account and use the platform end-to-end.

**Phase 5.5 — Sentinel (security broker, the load-bearing piece).**
This is the security backbone that must exist before the control-plane
Claude has **any external capability beyond opening PRs** (ADR-001
amendment: the PR-only v0 of Phase 4.5 may precede it; everything
else — Slack, MCP, SaaS, direct cluster writes — may not). It is
immediately preceded by the planned cluster rebuild: **Cilium** CNI
(Flannel does not enforce NetworkPolicy, which Sentinel requires),
per-node CPU caps, and `k3d/coredns-custom.yaml` reapplied.
Sentinel lives in a
*separate trust domain*: a small Python or Node service running as a
**systemd unit on the WSL2 host directly** — not inside k3d. The
cluster has no kubectl path, NetworkPolicy, or service account that
reaches Sentinel's admin API; strict one-way trust. Sentinel calls
into the cluster (proxy traffic), never the reverse.

Tight MVP scope for Phase 5.5:

- **Broker API.** `POST /capability-request` (called by Claude),
  `POST /capability-grant` (called by the GUI after a human tap),
  `GET /capability-check` (called by the Sentinel proxy on every MCP
  invocation). Issued tokens are short-lived (default 5 min) and
  scope-locked to a single MCP tool name + flow-id.
- **Sentinel proxy.** Sits in front of every MCP server. Built as an
  **Envoy `ext_authz` filter calling `/capability-check`** (ADR-001 —
  reuses the Phase 2.5 Envoy investment instead of hand-rolling a
  proxy). Validates flow-id + token + scope on every request.
  NetworkPolicy + mTLS ensure MCP servers refuse traffic that did not
  come through the proxy.
- **One-screen web GUI.** Shown when a capability request lands. Lists
  active flows, displays each request with context (flow-id, tool,
  reason, recent actions), one-tap **Grant 5m / Grant 1h / Deny**.
  Also contains the **global kill switch** — one button invalidates
  every outstanding token and refuses new ones until re-enabled.
- **Audit log.** Small SQLite or Postgres store on the WSL2 host.
  Every request, grant, denial, use, and revocation is logged with
  flow-id and timestamps. This is the canonical record.
- **Auth for the human.** WebAuthn / passkey if practical for MVP;
  TOTP as a fallback. Never password-only — phishing the password
  phishes the kill switch.

Deferred from MVP, on the roadmap: **capability profiles** (preset
bundles like `email-drafter` so common requests are one-tap), **trust
gradients** per namespace (sandbox auto-approves more, prod always
taps), **batched grants** (one tap for N invocations), **mobile PWA**,
**full audit dashboard**. These layer on later without architectural
change.

Sentinel is the contract every later component must respect: no
workload (control-plane Claude, MCP servers, future agents) holds
long-lived external credentials. They all request, get gated, and
release. This is the property that makes the platform safe to put
real powers behind.

**Phase 6 — the control-plane Claude (full powers).** Control-Plane
v0 (Phase 4.5) already proposes PRs; Phase 6 graduates it to the
end-state. Create namespace `platform-control`. Deploy a long-running
Claude Agent SDK workload that:
- Reads from Prometheus, Loki, and the Kubernetes API to observe state.
- Proposes actions by auto-committing PRs to the GitOps repo.
- Posts each PR to `#claude-approvals` in Slack with ✅/❌ buttons; on
  ✅ the bot merges and ArgoCD applies. The owner never types git.
- Logs every action to `#claude-audit` with reasoning.
- **Holds no long-lived credentials for external systems.** For any
  external action, Claude calls Sentinel's `/capability-request`,
  waits for a human grant, and uses the short-lived token via the
  Sentinel proxy. If Sentinel denies or the global kill switch is
  on, every external action fails closed.

Alongside, start the **MCP server catalog** in a sibling namespace
`mcp-servers`: each external system (GitHub, Slack, kubectl-wrapper,
later Supabase / Railway / Sendgrid / Google Workspace / etc.) is a
Deployment exposing a Model Context Protocol server. The control-plane
Claude auto-discovers MCP servers via a label selector but reaches
them only **through the Sentinel proxy** — direct pod-to-pod traffic
to MCP servers is blocked by NetworkPolicy. **Each MCP server still
ships with its own scoped tool allowlist** in its ConfigMap as a
defense-in-depth layer behind Sentinel: even if a Sentinel grant
accidentally matched a more powerful tool, the MCP server itself
would refuse. Three independent layers (Sentinel grant → MCP
allowlist → upstream OAuth scope) must all align for an action to
succeed.

**Phase 7 — observability completion.** kube-prometheus-stack already
landed in Phase 3; this phase completes the stack: **Loki** for logs
(+ **Tempo** for traces if budget allows). Grafana dashboards for:
cluster health, app metrics, LLM cost and usage (from the AI gateway's
token metrics), and a dedicated **Claude actions dashboard** showing
what the agent did, when, and how often each MCP server was invoked.
Wire Alertmanager → `#claude-alerts` for anomalies: secret reuse,
abnormal action rate, unexpected namespaces touched, kill-switch
flips.

**Phase 8 — cloud.** Provision a DigitalOcean Kubernetes (DOKS) cluster
using Terraform — not the web console; the IaC is the point. Apply the
same root ArgoCD Application; the entire `catalog/` deploys
unchanged. Enable the cluster autoscaler. Then `terraform destroy` it.
*(Reframed by ADR-002, 2026-07-26: this is a supported product path,
not a drill — "domain in, platform out" from one entry command; the
teardown is cost hygiene after proof, and keeping it up is a
documented choice. See `docs/adr/ADR-002-cloud-parity-contract.md`.)*

## Working principles

- Teach as you go: explain what each resource does and why. The goal is
  the owner learning, not just a finished result.
- Prefer declarative over imperative — manifests, Helm charts, and
  Terraform committed to git, not one-off `kubectl` commands.
- The control-plane bot commits to git on the owner's behalf with clear
  messages; the owner never types git. Slack approval messages must
  summarize each change in plain English first, with the diff as a
  clickable link second.
- Before applying anything to the cloud, confirm the cost and confirm the
  teardown step.
- If a step fails, diagnose and explain the fix rather than silently
  working around it.
- **Build for adopters (owner input 2026-07-25).** This repo will be
  public and must be *stupid-easy for a human* to install, maintain,
  and update — not just easy for Claude. Concretely: docs live
  in-repo and version with the code (no wiki drift); `README.md` is
  the front door (skeleton lands with Phase 4 when the install story
  is real, demo GIF at Phase 4.5); `docs/adr/` holds the whys;
  `docs/operator-cheatsheet.md` grows into day-2 operations docs.
  From Phase 2.5 onward, nothing new may hardcode owner-specific
  values (paths, IPs, usernames) — bootstrap detects or
  parameterizes them; sweeping the existing hardcoded values is
  backlogged. Updating the platform must reduce to `git pull` +
  ArgoCD sync; a one-command idempotent bootstrap script is a
  Phase 4 deliverable.
- **No agent action without a paper trail.** Every control-plane Claude
  action must (a) produce a git commit, (b) post to Slack for approval
  unless explicitly pre-authorized for the namespace, and (c) log to
  `#claude-audit`. There are no "quick fixes" that skip these gates.
- **MCP scoping and capability grants flow through Sentinel.** Claude
  never holds long-lived external credentials. Every external action
  is gated by a Sentinel capability check at the proxy — scope, TTL,
  and flow-id are validated on each call. Defense in depth: every MCP
  server *also* ships with its own tool allowlist in its ConfigMap
  (e.g. for Google Workspace: `gmail.read`, `gmail.draft` only — no
  `gmail.send`, no `docs.*`, no `drive.*`), and OAuth credentials at
  the upstream service are granted minimal scopes so even a
  compromised MCP server cannot exceed what its OAuth token permits.
  Three independent layers must all align for an action to succeed:
  upstream OAuth scope, MCP server allowlist, Sentinel grant.
- **Trust-domain separation is non-negotiable.** Sentinel — the
  credential broker, GUI, kill switch, and audit log — runs on the
  WSL2 host outside the k3d cluster. The cluster has no path to
  Sentinel's admin API. Claude can be told *no* by Sentinel and can
  observe denials, but cannot reach the API that grants. If a future
  feature seems to require Claude reaching the Sentinel admin
  surface, the design is wrong — reframe the feature so Claude
  *requests* and the human *grants* through Sentinel's existing
  flow. There is no exception to this rule, including for "trusted"
  or "internal" actions.

## Priority order if time runs short

Host + Docker (Phase 1) → chat baseline as catalog charts (Phase 2) →
gateway swap (Phase 2.5) → autoscaling + Prometheus (Phase 3) →
GitOps app-of-apps (Phase 4) → **Control-Plane v0, PR-only (Phase
4.5** — the demoable product) → team enablement (Phase 5) →
**Sentinel security broker (Phase 5.5, non-negotiable before any
non-PR external power)** → control-plane Claude full powers + MCP
catalog (Phase 6) → observability completion (Phase 7) → cloud
(Phase 8). Phases 1–4.5 are the demoable open-source core and a
respectable platform-engineering portfolio result on their own;
Phases 5–7 are what make this *cutting-edge* and where the novel work
lives. **Sentinel is the load-bearing security piece — if scope ever
gets cut, never grant any non-PR external power without Phase 5.5 in
place.** Phase 8 is the cloud-portability proof (and where replica
scaling becomes physically real) — run it only after a successful
local Phase 7.
