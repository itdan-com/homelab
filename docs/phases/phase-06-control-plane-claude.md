# Phase 6 — Control-plane Claude

**Goal:** Graduate Control-Plane v0 (the PR-only operator from Phase 4.5) to full powers: a long-running Claude Agent SDK workload in the `platform-control` namespace that observes the cluster and proposes actions via GitOps PRs + **Slack** approval, with every external access gated through Sentinel. The delta over v0 is exactly the parts that need Sentinel: Slack ChatOps, the MCP server catalog, and any non-PR external action.

**Status:** Not started. Blocked on Phase 5.5 (Sentinel) — **non-negotiable for every capability in this phase** (they are all non-PR powers; ADR-001).

---

## High-level outline

1. Build a custom Agent SDK container image (Python or Node) with: Claude SDK, Slack SDK, GitHub SDK, Sentinel client.
2. Write the system prompt: agent's role, decision criteria, escalation rules, audit format.
3. Deploy as a Deployment + ServiceAccount in `platform-control` namespace. RBAC: read-only on most namespaces, no write outside its own.
4. Wire to Prometheus, Loki, Slack, GitHub, Sentinel proxy.
5. Implement the propose-PR-and-await-approval flow:
   - Detect signal (e.g. CPU high) → propose action → commit branch → open PR → post Slack message with ✅/❌ → on ✅, merge → ArgoCD applies.
6. Start the **MCP server catalog** in namespace `mcp-servers`:
   - GitHub MCP, Slack MCP, kubectl-wrapper MCP first.
   - Each behind the Sentinel proxy.
   - Each ships with its own tool allowlist ConfigMap (defense in depth).
7. NetworkPolicy: block direct pod-to-pod traffic to MCP servers; only the Sentinel proxy can reach them.

## Which flow is this phase? Both — and they are built in this order

`CLAUDE.md` now names the two flows: **Mission Control** (the platform
operating itself under PR review) and **Airlock** (the workforce
reaching MCP servers under policy). Phase 6 finishes Mission Control
and lays Airlock's foundations. Order matters:

1. **ADR-005** — the policy and delegation model (Cedar + XAA).
2. **Elevation for the agent** — a capability *profile* covers a set of
   tools for a window, so one task costs one approval instead of six.
   This is Mission Control's last missing piece and it unblocks
   everything after it.
3. **First real MCP server** — GitHub, since the operator already uses
   it. Still in-cluster only; no public door yet.
4. **Control-plane Claude** in `platform-control`, with Slack
   approvals. **Mission Control is then complete.**
**Airlock is NOT in this phase.** It became **Phase 6.5** in its own
right (2026-07-28) — see `CLAUDE.md`'s build plan. Cramming a public
MCP door, gateway OAuth, Cedar entitlements and a multi-user Sentinel
into Phase 6 would have made it unshippable, and this repo already has
the convention for inserting half-phases. What Phase 6 owes Phase 6.5
is listed below, so the work done here does not have to be redone.

### What Phase 6.5 (Airlock) needs that does not exist yet

Naming these now so nobody assumes they are free:

- **A public MCP door.** MCP servers today are ClusterIP behind the
  proxy, reachable only in-cluster — correct for an in-cluster agent
  and useless for a human with Claude Desktop. Airlock needs
  `mcp.<domain>` on Envoy with real TLS.
- **Client authentication at the gateway.** Corrects an earlier claim
  in this repo that MCP servers need no client authorization because
  the proxy handles it: true for an in-cluster agent, incomplete for
  human clients. The *gateway* must authenticate real users (OAuth
  against Authentik) before Cedar can decide anything about them.
- **A principal that is a person, not a flow.** Sentinel's model today
  is `(flow_id, tool)`. Airlock needs `(user, group, server, tool,
  tier)` — which is exactly what Cedar is for, and the reason the
  policy engine cannot be deferred.
- **Recording rich enough to reconstruct.** The promise of an elevation
  window is that everything done inside it is recoverable after the
  fact. Sentinel's audit log records *that* a tool was called; Airlock
  wants enough context to reconstruct what changed and, where the
  upstream supports it, reverse it. Scope this honestly — full undo of
  arbitrary actions is not a thing; per-tool reversal sometimes is.

## Candidate: implement all three layers for real (owner ideas, 2026-07-27)

`CLAUDE.md` promises **three independent layers** that must all align
before an action succeeds — upstream OAuth scope, the MCP server's own
tool allowlist, and the Sentinel grant. Today only the middle one is
concrete. Two owner proposals would make the outer two real, and they
are complementary rather than competing:

**Layer 1 — XAA / Cross App Access** (`https://xaa.dev`). An OAuth
extension built on **ID-JAG** (Identity Assertion JWT Authorization
Grant, IETF draft), added to the MCP spec on 2025-11-25 as an
Authorization Extension (SEP-990). The IdP issues a short-lived, scoped
delegation token so an agent can act for a user against a downstream
app without static API keys or service accounts. That is exactly what
"upstream OAuth scope" should be, and it replaces the long-lived
credential the current design would otherwise park in a secret.

- **Does not conflict with Sentinel**, despite marketing that says it
  "replaces the consent screen": XAA answers *may this app, for this
  user, reach that resource at all* — enterprise policy, decided ahead
  of time. Sentinel answers *may this task invoke this tool right now* —
  ephemeral, per-flow, human. Different questions, different moments.
- **Constraint:** XAA makes the IdP the control plane, and ours
  (Authentik) runs *inside the cluster* with its groups declared in this
  repo, which the operator opens PRs against — the ADR-004 escalation
  shape exactly. So XAA needs an IdP the agent cannot reach: the
  xaa.dev playground for an MVP, or an external tenant. Also unverified:
  whether Authentik implements ID-JAG at all (doubtful).

**Layer 3 — Cedar for the capability decision.** Cedar has become the
dominant policy language for MCP authorization (ToolHive, IBM
ContextForge, AWS Cedar for Agents, Bedrock AgentCore Policy), and the
standard architecture is precisely ours: every tool call crosses a
gateway, so the gateway evaluates policy. **We already have that
gateway** — a per-MCP Cedar sidecar would be a second hop doing what
Envoy + ext_authz already does. The valuable move is replacing
`check_capability`'s hardcoded conditionals with a Cedar evaluation
inside the broker: same decision point, far more expressive.

- **This is probably the answer to what 5.5.8 measures.** If one honest
  MCP session needs four or five separate approvals, the one-grant-per-
  call model is unusable and humans will rubber-stamp — worse than no
  gate. The roadmap's answers, *capability profiles* and *trust
  gradients per namespace*, are both policy problems.
  `permit(...) when { resource.tier == "sandbox" }` IS a trust gradient.
- **Constraint, same shape as above:** policy that governs the agent
  must live where the agent cannot edit it. Cedar policies in
  `catalog/` would let the operator PR its own authorization wider as
  routine-looking config. Either keep them host-side beside the
  Sentinel database, or in a **second repo the operator's GitHub App is
  not installed on** — which keeps a review trail without handing over
  the pen. Recommend the latter.
- To verify at build time: a usable Cedar binding for Python (the
  engine is Rust), and whether ToolHive overlaps enough with our proxy
  to adopt rather than reinvent — doubtful, since none of these do
  human-in-the-loop per-flow grants, which is the part that is ours.

**Sequencing (agreed 2026-07-27):** run 5.5.8 first and get the real
approval count, *then* write one ADR covering both — designing the
policy model before measuring the problem is guessing. Together they
make the three-layer claim a working reference implementation rather
than three separate good ideas.

## Open questions to resolve at the start

- Agent loop pattern: cron-like ticks (`every 5 min`), or event-driven (Prometheus webhook fires alert → agent wakes up)? Probably **both** — heartbeat tick + alert handler.
- How does Claude get its Anthropic API key? Via Sentinel-issued ephemeral, or a one-time bootstrapped Secret with audit logging? **Recommendation: bootstrap Secret with rotation, audit logged.** Anthropic credentials are *Claude's* own credentials, not external SaaS — distinct from MCP credentials.
- Prompt engineering: how much memory does the agent retain across ticks? Options: stateless (re-read context each tick), summarized history, or full conversation. Tradeoff: token cost vs. continuity.
- Per-namespace pre-authorization: are there actions Claude can take in `sandbox` without Slack approval? (Likely yes — see [[control-plane-target]] memory for ChatOps trust gradient.)

## Phase exit criteria

- Agent runs continuously, restarts on failure.
- An induced high-CPU signal causes the agent to propose a scaling PR within 5 min.
- A Slack ✅ tap merges the PR; ArgoCD applies; pod count increases.
- An external action (e.g. drafting a Slack message via the Slack MCP) goes through Sentinel: grant request appears in GUI, grant → action executes, audit log records.
- `STATUS.md` updated.

## Notes captured during execution

- (empty)
