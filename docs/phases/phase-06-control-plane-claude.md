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
