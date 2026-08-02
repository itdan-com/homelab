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

**Status:** **7.1 DONE 2026-08-02** —
`docs/adr/ADR-005-airlock-policy-model.md` written (**Proposed**;
owner acceptance is the gate to 7.2). All four blockers resolved,
both forward decisions made (7.4 GitHub upstream, 7.5 Slack), all
four audit gaps assigned homes, ADR-004 amended with the carve.
Next: owner reads/accepts ADR-005, then **7.2 — capability profiles
+ multi-user Sentinel** in a fresh session.

**Origin:** named 2026-07-28 (briefly "Phase 6.5", promoted to a
full phase the same day). Everything below moved here from the
pre-re-cut phase-06 doc and the 2026-07-28 audit so it would not be
lost; this doc is the collection point until 7.1 turns it into
decisions.

---

## The five sessions (CLAUDE.md build plan)

1. **7.1 — ADR-005.** ✅ **DONE 2026-08-02** — Cedar policy model;
   XAA/ID-JAG delegation; how a *resource* is derived from a tool
   call; the approve-vs-self-elevate carve against ADR-004. Also
   decided here so later sessions don't spin: the GitHub MCP server's
   upstream + toolset (for 7.4) and Slack Socket Mode (for 7.5).
   Resolved the four blockers below → see
   `docs/adr/ADR-005-airlock-policy-model.md` (Proposed).
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
decision, is phase-06 item 6.4. Four survived, and **all four are
resolved by ADR-005 (2026-08-02):** 1 → Decision 3 (resource maps in
the policy repo, deny-closed, tier always total); 2 → Decision 6
(the carve; ADR-004 amended in place); 3 + 4 → Decision 5 with one
answer (the agent-unreachable policy repo carries the Cedar policies
*and* the entity store — group membership IS policy data). Kept
below for the record:

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

*All four assigned homes by ADR-005 ("Homes for the four audit
gaps"): SSE teardown → bounded + 7.3 stream-lifetime cap; tools/list
rewriting → accepted MVP limitation, whole-server invisibility via
handshake-scope policy is the real boundary; RP-ID → Phase 9
re-enrolment runbook; OAuth/DCR → resolved by verification (DCR is
deprecated in the 2026-07-28 MCP spec; static registration + CIMD
carry it; 7.3 serves RFC 9728).*

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

## Slack: Socket Mode (recorded 2026-07-28; formalized 2026-08-02)

Slack's interactive features normally call back to a public HTTPS
URL, which a lab behind a home router does not have. **Socket Mode**
is the way out: the app opens an OUTBOUND WebSocket and receives
events over it — no inbound ingress, and it fits the trust model
(another outbound-only path). In cloud, Socket Mode still works; a
public request URL becomes possible but is not required.

**Formalized by ADR-005 Decision 8, corrected by verification:** the
tools 7.5 actually needs (post, read history, list channels) are all
*outbound* Web API calls — **no inbound path of any kind is
required, so Socket Mode is not a 7.5 deliverable at all.** It
becomes relevant only if Slack must ever push to the platform
(events, buttons, slash commands), which the current design
deliberately avoids. The paragraph above stays as the recorded
trigger condition.

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

## Exit criteria (firmed by ADR-005, 2026-08-02)

Unchanged in substance from the sketch, now grounded: "the agent
cannot PR the policy that governs it" is Decision 5's repo split;
"one approval per honest session" is Decision 4's handshake-scope
birthright plus Decision 6's profile grants.

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

- **2026-08-02 (7.1):** The scope-mapping finding — blocker 3 was
  understated: not just group *membership* but the **claim-computing
  expressions** live in this repo's blueprints, so a PR can make the
  IdP assert any membership for anyone. This produced ADR-005's P1
  (authorization never derives from agent-writable state) and the
  entity-store-in-policy-repo design.
- **2026-08-02 (7.1):** Cedar proven executable on this host before
  the ADR claimed it: `cedarpy` 4.8.7 wheel (cp312 manylinux, no Rust
  toolchain) + a 5-case four-outcome ladder (permit / confirm /
  forbid-through-elevation / can't-even-ask ×2) — 5/5. PyPI trap for
  7.2: the dist is `cedarpy`; `cedar-py` and `cedarpolicy` don't
  exist.
- **2026-08-02 (7.1):** Authentik 2026.5.6 (which IS current
  upstream) verified: has RFC 8414 + OIDC discovery (door-sufficient),
  has NO RFC 8693 / ID-JAG / DCR — and DCR was *deprecated* by the
  2026-07-28 MCP spec revision anyway (CIMD + static registration
  won). Don't chase Authentik 2026.8's DCR (likely enterprise-gated).
- **2026-08-02 (7.1):** ToolHive v0.41.0 embeds Cedar (official
  cedar-go) as its default MCP authorizer — strong independent
  validation of the architecture — and verifiably contains no
  human-in-the-loop grant/TTL/elevation at any tier: the Sentinel
  primitive remains ours.
- **2026-08-02 (7.1):** GitHub MCP: official server has a native
  Streamable-HTTP mode (v1.8.0); GitHub App server-to-server auth is
  **stdio-only**, and there is **no server-side repo allowlist**
  (open #1685) → fine-grained PAT is the only repo-scoping lever in
  http mode. A `--read-only` bypass existed in http mode pre-1.0
  (fixed 2026-04-13) — 7.4 must negative-test the flag live.
- **2026-08-02 (7.1):** Slack ships an official MCP server
  (`mcp.slack.com`, GA 2026-02-17) but it is hosted-only SaaS → it
  bypasses the Sentinel proxy → rejected; korotovsky/slack-mcp-server
  self-hosted with an `xoxb` token chosen instead. Bot tokens cannot
  do classic search (user-token-only scope) — accepted limitation.
