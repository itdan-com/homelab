# ADR-007: Airlock hardening — velocity, live ownership, partial grants, access hygiene

**Status:** **ACCEPTED** (owner, 2026-08-15: *"lets get startedd?"*, after
a follow-on research pass deepened the case — see the Addendum below).
Written from a six-agent research audit run before continuing Phase 8,
checking ADR-005 as actually BUILT against the current (2026) industry
state rather than against what was known when it was written. Binding
from this point forward, same as every other accepted ADR in this
repo. Build order is priority order: Decision 1 (velocity) first.

## Addendum (2026-08-15) — prior art found for Decision 1 before it was built

A follow-on competitive survey (nine major providers, checked against
the same axes this ADR already cares about) turned up two findings
worth recording before Decision 1's implementation starts, so the
build session doesn't rediscover them the hard way:

- **AWS shipped the literal mechanism this decision proposes, six days
  after it was written.** On 2026-08-06, AWS released "temporal
  policies" for Bedrock AgentCore, powered by a new Apache-2.0 language
  called **Dogwood** (`github.com/dogwood-policy/dogwood`) that
  *extends Cedar itself* with native rolling-window operators —
  `count_within`, `count_distinct_within`, `sum_within` — as first-class
  conditions inside an ordinary Cedar `when` block, evaluated by the
  same engine that makes the permit/forbid call. This is not "a
  competitor thought of this too": it is `context.actions_in_window`,
  already designed, in our own policy language. The maintainers'
  own README says the reference interpreter isn't production-ready —
  treat it as grammar and semantics to study before designing ours,
  not a dependency to pull in.
- **Independent third confirmation of the underlying architecture**:
  Microsoft Entra ID Protection for Agents runs a first-class "Agent
  risk" condition (sign-in-frequency spikes) directly inside
  Conditional Access policy authoring, not as a bolted-on dashboard —
  agreeing with the PAM/UEBA research this decision was already built
  on. Honest caveat carried forward: Microsoft's own docs say those
  specific detections run async/offline, which is exactly the
  sync-threshold-vs-async-behavioral-baseline line this ADR already
  drew, on the side this decision deliberately stays on.

Also found, filed to the backlog rather than this ADR (different shape
of gap — visibility filtering, not a policy-decision axis): AWS
Bedrock AgentCore Gateway independently solves ADR-005's named "gap 2"
(tool visibility) through the same Cedar engine we run, via a
dedicated `bedrock-agentcore:PartiallyAuthorizeActions` permission —
a second, more directly portable reference alongside Okta's
session-scoped tool registry.

**Owner input (2026-08-04), the question that triggered this ADR, in
their words:** *"MOST MCP Servers are garbage… enterprises need
authentication, authorization, accounting, then human in the loop, and
beyond authN/authZ there's a need for more nuance than that."* Three
concrete scenarios given as the nuance: **(1)** wiping one laptop is
routine, wiping 100 is an incident — *"how do we decide to interpret
velocity?"* **(2)** a support engineer's IdP role grants broad
visibility and cross-ticket write access; an LLM inheriting that whole
role via OAuth is *"chaos,"* even though the same access is fine in
the human's own hands. **(3)** some actions should require evidence of
an external record first — *"you're only allowed to modify a firewall
if you have a change request."* Plus a scope-granularity complaint:
*"claim handling — I don't get to ask for 6 and get 4, it's largely
approve or deny"* — and a backend-asymmetry example: Snowflake can
scope a role down to specific database access; Databricks, by the
vendor's own limitation, hands back read+write always, so Cedar alone
can't add granularity a backend doesn't expose without inspecting the
call itself.

## Context

Phase 7 (Airlock) is BUILD COMPLETE and ADR-005 already answers most
of this: Decision 3's per-tool resource-tier map, Decision 4's
three-context Cedar ladder (baseline → elevated → approved), and
Decision 10's identity ladder + THE RULE (write tools need per-user
credentials or better) are real, shipped, audited mechanisms. Rather
than guess which of the owner's scenarios were already covered, six
research agents independently verified each one against current specs,
RFCs, and named vendors, with the actual ADR-005 decisions as the
comparison baseline. Two of the owner's three scenarios turned out
**already solved**; the third, plus the scope-granularity complaint,
surfaced **four real, proportionate gaps**. This ADR records both
halves so the "already solved" reasoning isn't re-litigated later, and
decides the four gaps.

## Confirmed correct — no change

- **Scenario 2 (support-engineer OAuth over-reach) is answered by
  ADR-005 Decision 10's THE RULE**: a server with write tools must use
  per-user credentials or better, specifically so the *upstream*
  enforces that person's own permissions — someone who can't touch a
  ticket in the native system can't touch it through Airlock either.
  Verified against 2026 industry data (Gravitee's 2026 State of AI
  Agent Security report): ~70% of enterprise AI agents exceed the
  access level of an equivalent human role — THE RULE is written to
  make that number zero here. Decision 3 below is what completes it
  (bounding by *credential* is necessary but not sufficient — see
  Decision 3's "assigned to me" gap).
- **Snowflake-vs-Databricks (backend-asymmetry) is answered by
  ADR-005 Decision 3.** The resource-map/tier mechanism is already
  built to extract a nameable resource per call and evaluate it as a
  tiered Cedar entity ("DROP TABLE forbidden on prod, permitted on
  staging"). Databricks-shaped backends that don't expose a nameable
  resource at all get `tier: unclassified` today, honestly, per
  Decision 3's deny-closed rule — there is no backend-blind trick that
  fixes this; a resource map can only extract what the call and the
  backend can express.
- **Elicitation is not, and must not become, an authorization
  channel.** MCP's own human-in-the-loop primitive (`elicitation/create`,
  stable as of the 2026-07-28 spec, reworked by SEP-2322) is a
  server-authored, unsigned prompt with no attestation that its text
  matches the actual risk — the spec's own security section says
  servers **MUST NOT** use it for sensitive decisions, and documented
  MCP tool-poisoning research (Invariant Labs; MCPTox, up to 72.8%
  attack success) shows that exact channel gets exploited. Sentinel's
  console — independent channel, authoritative data, model never
  touches the link — is the correct design, confirmed rather than
  merely defended. No change. (Optional, non-load-bearing: elicitation
  is fine for a pre-Sentinel disambiguation question like "staging or
  prod bucket?" — cosmetic only, never gating a decision.)
- **Cedar over a dedicated ReBAC engine (OpenFGA/SpiceDB) for this
  system's shape.** Examined directly because Scenario 2/3 look
  relationship-shaped. OpenFGA's own docs route fast-changing
  relationship data (exactly this system's problem) around its synced
  store via request-scoped "contextual tuples" rather than sync it —
  the leading ReBAC engine gives the same answer this ADR reaches in
  Decision 2. Standing up a second engine would add a second
  consistency model and a second place truth can drift, for a
  flat-lookup problem, not the multi-hop graph problem ReBAC engines
  exist for (SpiceDB backs ChatGPT Enterprise's connector permissions
  at that scale; this system isn't there).
- **The broker+proxy+short-lived-scoped-token shape.** Verified as
  structurally the same pattern the ecosystem converged on
  independently — ToolHive (default authorizer is Cedar, via
  `cedar-go`), Teleport's Agentic Identity Framework (Jan 2026),
  WorkOS AuthKit / Descope / Cloudflare's MCP-auth stacks. Confirmed,
  not a lab approximation of the real thing.

## The gaps — four decisions, each an extension of a mechanism already built

### Decision 1 — Velocity as a fourth Cedar context flag

**The highest-priority gap**, named independently by two research
streams as the thing the industry calls out as still-missing even in
designs that already have short-lived scoped grants (Gravitee 2026,
CrowdStrike's "Continuous Identity for AI Agents").

Sentinel computes `context.actions_in_window` — a synchronous count
keyed on `(principal, tool-or-profile, resource-tier)` over a rolling
window (`_1m` / `_1h` suggested) — from its own audit log, immediately
before each Cedar evaluation, exactly like the existing baseline →
elevated → approved passes. This is the industry-standard shape, not
an improvised one: NIST SP 800-207's PDP/PEP/**PIP** model treats a
velocity signal as one more attribute a Policy Information Point
supplies to the PDP, and payments/fraud "velocity checks" are
universally synchronous and inline for the same reason we want this —
to stop action N before it completes, not alert after it. It composes
with forbid-trumps-permit unchanged: `forbid wipe_laptop when
context.actions_in_window._1h > 5`.

**Explicitly out of scope for this decision:** ML-driven behavioral
baselining (unusual *for this specific principal*, or vs. a peer
group) — that's what CyberArk/Delinea/Entra reserve for a separate,
async, history-heavy component, and building it now would be solving a
threshold-counter problem with infrastructure it doesn't need. Simple
counting now; the async version is a named future upgrade if the
threshold approach proves too blunt.

**Two implementation notes, so they aren't found the hard way later:**
this count now runs on *every* call, not just forensics — the audit
log needs an index on `(principal, tool_or_profile, ts)` before this
ships, or it becomes the hot-path latency problem the rest of the
design was careful to avoid. And because a call's outcome can now
change between two evaluations a minute apart (confirm-tier now,
approve-tier a minute later because volume crossed a threshold), the
client-facing `elevation-available` hint (Decision 4, ADR-005) must be
read fresh each time, never cached as "this profile is confirm-tier" —
test this explicitly.

### Decision 2 — Live-attribute resolver on the resource map (closes Scenarios 2 and 3 together)

One mechanism for two of the owner's scenarios. For tools whose
resource map already names a tier-classified resource, the map gains
an optional **live-attribute resolver**: Sentinel makes a read-scoped
call to that backend's own API (ticket assignee, asset owner, whether
an approved change record references this resource) *immediately
before* the Cedar evaluation it's feeding, and passes the answer in as
request-scoped entity attributes — `resource.assigned_to`,
`resource.assigned_team`, `resource.has_open_change_record` — never
persisted, never synced, gone after the call. Policy then reads:

```
permit if principal == resource.assigned_to || principal in resource.assigned_team
forbid modify_firewall_rule unless resource.has_open_change_record
```

This is architecturally identical to OpenFGA's own "contextual tuples"
answer for exactly this staleness problem, and Cedar/AWS Verified
Permissions already supports it natively — `IsAuthorized` takes
entities and attributes inline per call with no requirement they be
pre-loaded into a persisted store. No new engine, no policy-model
change, the static admin-authored people-to-group hierarchy is
untouched.

**The constraint that makes this safe rather than a new hole:** the
resolver call must ride the same per-user/delegated credential THE
RULE already requires for that tool's write path — never a separate,
more-privileged "read anything to check ownership" credential. A
shared elevated credential used only to answer "who owns this" would
quietly recreate the exact over-broad-access problem Decision 10 and
this decision exist to prevent.

This decision is also a **prerequisite, not just a nice-to-have**, for
the roadmap's next MCP servers (the "common enterprise set" backlog
item names Snowflake and peers, and any future ticketing/asset-
management server) — anything with owner-scoped resources needs this
from day one of onboarding, or it ships with the Scenario-2 hole open
by default.

### Decision 3 — Approve-tier grants can be edited before granting, not just accepted or denied

Answers the "I don't get to ask for 6 and get 4" complaint honestly.
RFC 9396 (Rich Authorization Requests) does have a native partial-grant
mechanism (Section 3: "the user may also grant a subset of the
requested authorization details") — but it is unilateral and one-shot
(the AS decides, the client is informed after, no negotiation
round-trip), has essentially no adoption in agentic/MCP tool-calling
(the MCP-side feature request is open and unimplemented:
modelcontextprotocol/modelcontextprotocol#1670), and is scope-level,
never quantity-level — "4 of 6 laptops" was never expressible as an
OAuth scope regardless of maturity. Adopting RFC 9396 for this would
be protocol theater.

The pattern that actually ships this kind of narrowing lives one layer
up, at the human-console level — LangChain's `HumanInTheLoopMiddleware`,
the OpenAI Agents SDK's `require_approval`, and Microsoft's Agent
Governance Toolkit all let the approving human edit pending arguments
before dispatch. Extend Sentinel's **approve-tier console only**
(leave one-tap confirm alone — per the taps-per-action budget, that
path can't absorb friction) so the approving human can strike
items/verbs from an enumerable request before granting — remove
specific IDs from a batch, drop `DELETE` from a requested
`[SELECT, DELETE]` profile — store the edited set as what actually
executes, and audit both requested-and-granted.

This is scoped to the deliberately rare `approve` path only, and it's
a GUI feature — see the console-suggestions writeup for where it
lands in the pending-request card.

### Decision 4 — Lightweight access reconciliation (IGA-lite), and one addendum to THE RULE

Named-real gap, wrong-sized as a vendor buy. CSA's 2026 "Non-Human
Identity Governance Vacuum" whitepaper gives the numbers: NHI:human
ratio 144:1 (up from 92:1 in under two years), 1-in-20 non-human
identities admin-privileged and inactive 9+ months, 47% unchanged over
a year, 51% with no clear ownership. That's the exact failure pattern
a confirm/approve grant system produces if nothing ever looks back at
what it issued — and it's real regardless of scale. But Astrix/Oasis/
Entro and platform IGA (SailPoint's Agentic Fabric, Saviynt) are
priced and built for thousands-of-NHI, multi-SaaS estates with a
dedicated IAM team — the wrong tool for one hand-authored entity
store.

The right-sized version, using data Sentinel already logs: a monthly
job (cron, or a Phase-8 Prometheus recording rule) reading the audit
log against the current access matrix, flagging **(a)** birthright
`permit` entitlements with zero invocations in 90 days, **(b)**
confirm/approve grants for the same `(principal, profile)` renewed
back-to-back across windows — standing access hiding behind a timer,
the exact anti-pattern CSA names — and **(c)** tier-3/4 shared
credentials past a stated max-age with no rotation. Output routes
through the PR gate Mission Control already has, because the fix is
always a declarative access-matrix or `servers.yaml` edit — a diff in
front of a human, not a Slack button.

**Addendum to ADR-005 Decision 10 (THE RULE):** the identity ladder
states which credential tier a server must use but names no
rotation/max-age policy for tier-3 shared credentials. Add one line:
*tier-3 shared credentials get a stated rotation interval, checked by
this same reconciliation job.* Small, but it's the gap the same CSA
research flags sitting right next to the rule it's amending.

## Also verified, not actionable now — worth knowing, not worth a decision

- **If elicitation is ever adopted even for the cosmetic disambiguation
  use in "Confirmed correct" above:** its `message`/schema
  `description`/`enumNames` fields are documented prompt-injection
  surface (Simon Willison's MCP prompt-injection writeup; the MCP-38
  threat taxonomy, arXiv 2603.18063) because clients render them as
  trusted UI and models re-ingest the response as context. Don't wire
  elicitation output back into a model's context unsanitized, even for
  a "harmless" clarifying question.
- **The 2026-07-28 MCP spec made MCP stateless** (no `Mcp-Session-Id`)
  and added RFC 9207 issuer validation. Worth a compliance sanity check
  against the 7.3 door in a future session — not audited here, not
  assumed broken, just not yet checked against the newest revision.
- **7.3's door hand-rolled its own minimal AS facade** (CIMD, no DCR)
  because nothing off-the-shelf did exactly that in 2026-07 when it was
  built. WorkOS AuthKit, Descope, and Cloudflare have since started
  shipping "become an MCP-compliant OAuth 2.1 AS" as a checkbox. Not a
  rebuild — the owner's no-DCR security stance and CIMD choice were
  deliberate (ADR-005 Decision 9) — but worth knowing for anyone
  replicating this stack from scratch today: the door might not need
  to be hand-built next time.

## Consequences — priority order

Implementation order, reasoning tied to the owner's stated top
priority (unattended operation: *"scale effortlessly at 3am, open a PR
I mosy out of bed for, approve it, and the company arms of
MCP-servers/bots keep going"*):

1. **Decision 1 (velocity)** first. Smallest, most self-contained
   change (one new context flag, no new data source), highest leverage
   for exactly the unattended scenario — it's the one control that
   stops a runaway/compromised agent's bulk action before a human wakes
   up, and it slots directly into the Phase 8 Alertmanager
   ("abnormal action rate") work already in flight this session.
2. **Decision 2 (live-attribute resolver)** second. Generalizes to two
   scenarios at once, completes Scenario 2 from "bounded by credential"
   to "fully closed," and is a prerequisite for onboarding any future
   ownership-scoped MCP server (the roadmap's Snowflake-and-peers item)
   — doing it now means new servers don't retrofit it later.
3. **Decision 4 (access reconciliation)** third. Detective, not
   preventive, so lower urgency than 1 and 2; cheap and independent
   (reads existing audit data, touches no hot path); pairs naturally
   with Phase 8 observability work already underway.
4. **Decision 3 (partial grants)** last. Touches only the
   already-rare, already-heavy `approve` path — no unattended scenario
   is blocked by not having it yet — and it's fundamentally a console
   feature, so it's the natural thing to build alongside the GUI
   visual pass rather than before it (building it on top of the
   current pending-card layout means restyling it twice).

- **7.2/7.3's schema and broker already carry everything Decisions 1–2
  need** (audit log with timestamps, resource-map extensibility,
  per-caller credential lookup) — no new trust domain, no new
  component, consistent with how ADR-005 itself is built (P2: one
  policy set, evaluated under different context flags).
- **STATUS.md backlog** carries all four as scheduled-not-yet-built
  items, this ADR as the design authority for each.

## Alternatives considered

- **A live-synced Cedar entity feed for ownership data.** Rejected —
  recreates Zanzibar's "new enemy problem" (a stale synced ACL granting
  access already revoked) without any of the zookie/zed-token
  consistency machinery Google built to solve it. Cedar has none of
  that machinery and shouldn't grow it for this.
- **OpenFGA or SpiceDB as a second policy engine.** Rejected as
  substrate for the same reason ToolHive was rejected in ADR-005 —
  real, well-built tools, wrong shape for this problem: a second
  consistency model and a second place truth can drift, to solve a
  flat per-call lookup that Cedar's existing inline-attribute contract
  already handles.
- **RFC 9396 / `authorization_details` for partial grants.** Rejected —
  unilateral not negotiated, unimplemented for MCP, scope-level not
  quantity-level. See Decision 3.
- **A dedicated NHI/IGA vendor (Astrix, Oasis, Entro, SailPoint
  Agentic Fabric).** Rejected on proportionality — built and priced for
  an NHI population three orders of magnitude larger than this
  platform's. See Decision 4.
- **Elicitation as (or feeding) an approval decision.** Rejected —
  unsigned, unattested, server-authored content; the spec's own
  MUST-NOT and documented tool-poisoning research both say no. See
  "Confirmed correct."
- **An async, ML-driven anomaly-detection service for velocity, built
  now.** Rejected as premature — a threshold counter answers the
  scenario given; behavioral baselining is real but heavier
  infrastructure than the stated problem needs. Named as a future
  upgrade path if simple thresholds prove too blunt, same shape as
  Decision 10's identity ladder being a named upgrade path rather than
  a thing built ahead of need.

## Non-goals

- Universal cross-tool undo (already a non-goal in ADR-005; unchanged).
- A general-purpose scope-negotiation protocol. Decision 3 is a console
  feature scoped to the rare approve path, not a new protocol layer.
- Peer-group / self-baseline behavioral anomaly detection. Named as a
  future upgrade to Decision 1, not built here.

## Sources (dated, as verified 2026-08-04)

MCP elicitation: modelcontextprotocol.io spec 2026-07-28, SEP-2322,
SEP-2260; client support via canimcp.dev compatibility matrix; Claude
Code 2.1.76. ReBAC: openfga.dev "contextual tuples" docs; AuthZed
zed-token/"new enemy problem" writeup; AWS Verified Permissions
`IsAuthorized`/`BatchIsAuthorized` API docs. RFC 9396 (May 2023);
modelcontextprotocol/modelcontextprotocol#1670 (open, unimplemented);
LangChain `HumanInTheLoopMiddleware`; OpenAI Agents SDK
`require_approval`; Microsoft Agent Governance Toolkit. NHI governance:
CSA "Non-Human Identity Governance Vacuum" whitepaper and AI Controls
Matrix v1.1 (July 2026); NIST CAISI AI Agent Standards Initiative (Feb
2026); SailPoint Agentic Fabric; Saviynt NHI+JIT convergence; cremit.io
NHI vendor tracking (2026). Velocity: NIST SP 800-207 (PDP/PEP/PIP);
CyberArk Identity Security Intelligence; Delinea Privileged Behavior
Analytics; Okta ThreatInsight; Microsoft Entra Continuous Access
Evaluation. MCP ecosystem posture: Practical DevSecOps / MintMCP 2026
MCP-server-security surveys; Invariant Labs tool-poisoning disclosures
(2025); Simon Willison's MCP prompt-injection writeup; MCPTox (arXiv
2508.14925); OWASP MCP Top 10 (MCP03:2025); Gravitee 2026 State of AI
Agent Security report; ToolHive v0.41.0; Teleport Agentic Identity
Framework (Jan 2026); WorkOS AuthKit / Descope / Cloudflare MCP-auth
docs.
