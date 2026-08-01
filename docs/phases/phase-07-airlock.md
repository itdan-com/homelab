# Phase 7 — Airlock (the workforce reaches the MCP servers)

**Goal:** anyone in the company points an MCP client (Claude
Desktop, an IDE) at one address, signs in with the company identity,
and immediately has the tools their role should have — birthright
entitlements at zero approvals. Consequential power is **borrowed**:
a `sudo`-shaped, time-boxed, recorded self-elevation (`confirm`),
with `approve` rare by design and `forbid` having no button at all.
The gate is elevation, not per-call approval — you cannot diff an
action that already happened, and a queue nobody reads manufactures
the appearance of oversight. See `CLAUDE.md` → "Two flows".

**Status:** Not started. Blocked on Phase 6 (Mission Control
complete). First work item: **7.1 — ADR-005.**

**Origin:** named 2026-07-28 (briefly "Phase 6.5", promoted to a
full phase the same day). Everything below moved here from the
pre-re-cut phase-06 doc and the 2026-07-28 audit so it would not be
lost; this doc is the collection point until 7.1 turns it into
decisions.

---

## The five sessions (CLAUDE.md build plan)

1. **7.1 — ADR-005.** Cedar policy model; XAA/ID-JAG delegation; how
   a *resource* is derived from a tool call; the
   approve-vs-self-elevate carve against ADR-004. Also decided here
   so later sessions don't spin: the GitHub MCP server's upstream +
   toolset (for 7.4) and Slack Socket Mode (for 7.5). Must resolve
   the four blockers below.
2. **7.2 — Capability profiles + multi-user Sentinel.** A grant
   covers a SET of tools for a WINDOW (schema change + policy
   evaluation in `check_capability`) — this is where 5.5.8's
   six-approvals-for-one-session finding is retired. Tenant scoping
   (the owner column, ADR-004 debt 1) lands at the START of this
   session, before real flows accumulate. Consider per-grant and
   per-flow revoke (debt 4) while the schema is open.
3. **7.3 — The public MCP door + gateway OAuth.** `mcp.<domain>` on
   Envoy with real TLS; OAuth/OIDC against Authentik so the gateway
   knows which PERSON is calling. Corrects the earlier in-repo claim
   that MCP servers need no client authorization because the proxy
   handles it — true for an in-cluster agent, incomplete for humans.
4. **7.4 — The GitHub MCP server, with XAA.** Its own session;
   upstream and toolset decided in 7.1.
5. **7.5 — The Slack MCP server.** Socket Mode (below). This is also
   where `#claude-audit` / `#claude-alerts` become real channels.

## ADR-005 must resolve — blockers from the 2026-07-28 audit

Recorded before the re-cut as five. **Original #1 — "no identity can
approve the agent's PRs" — DISSOLVED on 2026-07-28:** the approver
is a human with their own GitHub account, and that is the feature
(GitHub 422s self-approval unconditionally, so "the bot merges on
✅" was never implementable). Its Phase 6 residue, the admin-bypass
decision, is phase-06 item 6.4. Four survive:

1. **Cedar has no *resource*, so three of the four outcomes are
   unexpressible today.** `app/scope.py` derives `<server>.<tool>`
   and deliberately discards `params.arguments`; `check_capability`
   compares tool and flow only. So "`DROP TABLE` forbidden on prod,
   permitted on staging" — the flagship example — is the SAME
   capability string at the enforcement point. Decide how resource
   identity is derived from a tool call (per-server adapter? a
   declared `resourceKey` per chart? argument extraction with an
   allowlist?) — and that decision adds a column to `audit_events`.
2. **There is no `confirm` primitive, and its natural home is
   currently forbidden.** `confirm` means the caller elevates
   THEMSELVES from their own client, authenticated as an Authentik
   user. Grant/deny today live only on the loopback admin listener
   behind an operator passkey, and ADR-004 settles "Sentinel behind
   Authentik SSO: **no**" with three reasons. Carve it explicitly —
   **who may APPROVE stays local passkeys; who may SELF-ELEVATE
   comes from the IdP** — and amend ADR-004, or the two documents
   disagree from day one.
3. **Authentik group membership is a second self-granting path.**
   ADR-004 proved the agent can PR its own approval power via
   Authentik blueprints; the identical argument applies to
   entitlements: a routine SSO-housekeeping PR adds a principal to a
   privileged group, and Cedar then permits by birthright with no
   Sentinel surface touched. Deciding where Cedar policy lives does
   NOT cover where group membership lives.
4. **Where policy lives: where the agent cannot PR it.** Cedar
   policies in `catalog/` would let the operator widen its own
   authorization as routine-looking config. Host-side beside the
   Sentinel database, or a second repo the operator's GitHub App is
   not installed on (recommended — keeps a review trail without
   handing over the pen).

## Further gaps from the same audit (do not rediscover expensively)

- An elevation window does **not** tear down an already-open
  server-push channel: ext_authz runs per request; the kill switch
  cannot close an established SSE stream.
- Per-user **tool visibility** requires rewriting the `tools/list`
  RESPONSE, which an ext_authz filter structurally cannot do — needs
  a different mechanism or an accepted, stated limitation.
- The WebAuthn **RP ID is `localhost`** — every enrolled passkey
  dies the day Sentinel serves a real domain. Plan re-enrolment as
  part of the cloud story, not as a surprise.
- MCP clients expect **OAuth protected-resource metadata** and
  probably **Dynamic Client Registration**; nothing here implements
  either, and Authentik support is unverified — verify in the same
  spike as ID-JAG.

## Slack: Socket Mode (recorded 2026-07-28; formalize in 7.1/7.5)

Slack's interactive features normally call back to a public HTTPS
URL, which a lab behind a home router does not have. **Socket Mode**
is the way out: the app opens an OUTBOUND WebSocket and receives
events over it — no inbound ingress, and it fits the trust model
(another outbound-only path). In cloud, Socket Mode still works; a
public request URL becomes possible but is not required.

## The three layers for real: XAA (layer 1) + Cedar (layer 3)

Owner ideas 2026-07-27, researched pre-re-cut and kept here.
CLAUDE.md promises three independent layers that must align —
upstream OAuth scope, the MCP server's own allowlist, the Sentinel
grant. Today only the middle one is concrete.

**Layer 1 — XAA / Cross App Access** (`https://xaa.dev`), built on
ID-JAG (Identity Assertion JWT Authorization Grant, IETF draft; in
the MCP spec since 2025-11-25 as SEP-990). The IdP issues
short-lived, scoped delegation tokens so an agent acts for a user
downstream without static keys. Does not conflict with Sentinel: XAA
answers "may this app, for this user, reach that resource AT ALL"
(enterprise policy, decided ahead of time); Sentinel answers "may
this task use this tool RIGHT NOW" (ephemeral, per-flow, human).
Constraint: XAA makes the IdP the control plane, and in-cluster
Authentik is PR-able by the operator (the ADR-004 escalation shape)
— so XAA needs an IdP the agent cannot reach: the xaa.dev playground
for an MVP, or an external tenant. Also unverified: whether Authentik
implements ID-JAG at all (doubtful).

**Layer 3 — Cedar for the capability decision.** The dominant MCP
policy language (ToolHive, IBM ContextForge, AWS Cedar for Agents,
Bedrock AgentCore Policy), and the standard architecture is ours —
every tool call already crosses our gateway. The move is replacing
`check_capability`'s conditionals with a Cedar evaluation inside the
broker (same decision point, far more expressive), NOT a per-MCP
sidecar (a second hop doing what Envoy + ext_authz already does).
`permit(...) when { resource.tier == "sandbox" }` IS a trust
gradient. Verify at build time: a usable Cedar binding for Python
(the engine is Rust), and whether ToolHive overlaps enough to adopt
rather than reinvent — doubtful, since none of these do
human-in-the-loop per-flow grants, which is the part that is ours.

## What Airlock needs that Phase 6 does not provide

From the pre-re-cut doc, so nobody assumes they are free: the public
door; gateway-level client authentication; a principal that is a
PERSON (`(user, group, server, tool, tier)`), not a flow; recording
rich enough to reconstruct an elevation window — and, where the
upstream tool supports it, reverse it (universal undo is not a
thing; scope honestly).

## Exit criteria sketch (firm these up in 7.1)

- A person with an MCP client and company SSO reaches their
  birthright tools with ZERO approvals.
- A `confirm` elevation unlocks a tool SET for a window from the
  caller's own client; it expires by itself; the audit log
  reconstructs what happened inside it.
- An `approve` case requires a different human and is rare; a
  `forbid` tool has no path on the prod tier while the same tool
  works on staging.
- One honest MCP session costs at most ONE approval (5.5.8 measured
  six).
- The agent cannot PR the policy that governs it, nor its own group
  membership.

## Notes captured during execution

- (empty)
