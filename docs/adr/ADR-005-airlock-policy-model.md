# ADR-005: Airlock's policy model — Cedar, identity, resources, and the elevation gate

**Status:** **Proposed** 2026-08-02 (Phase 7.1); **amended same day
after owner review** — policy authoring moved from human PRs to the
Sentinel console (Decision 5); git demoted from authoring surface to
automatic memory. Needs owner acceptance before 7.2 (schema + policy
engine) begins.
**Owner input (2026-08-02):** the elevation model in their words —
person → group → birthright; elevation is a *request that is
auto-approved because the group already entitles it*, time-boxed
(30 min / 1 h / 2 h); a group without a server assigned cannot even
*request* elevation on it; sub-groups layer inside a role (head of HR,
recruiter, HR admin); on sources of truth, prefer no write path at all
over a rarely-used approval; an "ask for access you don't have"
workflow is explicitly out of scope for the build.
**Second owner input, same day:** no new manual PR flow for policy —
an admin configures groups, permissions, and birthright in a GUI
("read only for these groups, write for those, or write only via
request, and everything is audited"), simple enough that automation
could draft changes.

## Context

Airlock is the second flow: every employee points an MCP client at one
address, signs in with the company identity, and has the tools their
role should have. CLAUDE.md defines four outcomes — **permit** /
**confirm** / **approve** / **forbid** — and Phase 5.5 built the
enforcement chain that will carry them: every MCP call crosses an
Envoy ext_authz callout into the Sentinel broker (`/v1/ext-authz` →
`check_capability`).

What exists is necessary but not sufficient:

- `check_capability` is four hand-written conditionals: kill switch →
  token hash → exact `(tool, flow_id)` match → expiry. One grant
  covers **one tool for one call** — 5.5.8 measured six approvals for
  a single honest MCP session, which at workforce scale is the queue
  nobody reads.
- The principal is a **flow**, not a **person**. No table has an owner
  column (ADR-004 debt 1); nothing anywhere knows who a caller is.
- There is **no resource**. `app/scope.py` derives `<server>.<tool>`
  and deliberately discards `params.arguments`, so "`DROP TABLE` on
  prod" and "`DROP TABLE` on staging" are the same capability string —
  three of the four outcomes are unexpressible at the enforcement
  point.
- `confirm` has no home: granting lives only on the loopback admin
  listener behind an operator passkey, and ADR-004 settles "Sentinel
  behind Authentik: no."

This ADR decides the policy model that closes those gaps, plus the two
forward decisions (GitHub MCP upstream for 7.4, Slack transport for
7.5) so those sessions don't spin.

## The threat that shapes everything

While grounding this ADR, one fact turned out to be sharper than the
audit that raised it. Blocker 3 said Authentik group *membership* is a
second self-granting path because groups are declared in this repo's
blueprints. The reality is worse: **the scope-mapping expressions are
in the blueprints too** — the Python snippets that compute the
`roles`/`groups` claims (e.g.
`[g.name for g in user.ak_groups.all() if g.name.startswith("grafana-")]`
in `catalog/authentik/templates/oidc-blueprints.yaml`). A
routine-looking SSO-housekeeping PR can make the IdP *claim anyone is
in any group*, without touching a single group object. Reviewing
diffs does not save us, because the dangerous diff does not look
dangerous (the exact failure mode ADR-004 reason 1 names).

The generalization, and this ADR's first principle:

> **P1 — Authorization must not be derivable from any state the agent
> can write.** The operator's reach is: this repo (via PR), everything
> ArgoCD applies from it, and everything running in the cluster.
> Therefore nothing in that set may be the *source* of an Airlock
> authorization decision. The in-cluster IdP may prove **who** a
> caller is; it must never decide **what they may borrow**.

And its working partner:

> **P2 — One policy set, evaluated under different context flags,
> yields all four outcomes.** `permit` and `forbid` are direct Cedar
> answers; `confirm` and `approve` are the *distance between* the
> baseline evaluation and one with `elevation`/`approval` context set.
> There is no second policy language for elevation and no separate
> "requestability" table — eligibility to borrow is itself a Cedar
> decision.

## Decision 1 — Cedar, evaluated inside the broker

Cedar is the policy engine, evaluated **in the broker's
`check_capability`**, replacing its conditionals at the same decision
point. Not a sidecar in front of each MCP server (a second hop doing
what Envoy + ext_authz already does), and not a fork of the
enforcement chain: the kill switch and token liveness checks stay
exactly where they are, *before* policy — kill first, then policy.

**Engine and binding, verified on this host (2026-08-02).** The
binding is **`cedarpy`** (PyPI dist name; the GitHub repo is
k9securityio/`cedar-py` — and `cedar-py`/`cedarpolicy` do not exist
on PyPI, so nobody should pip-install a guess). v4.8.7 (2026-07-10),
Apache-2.0, prebuilt wheels — **installed clean on this exact host
(cp312 manylinux x86_64), no Rust toolchain**. The API is the whole
need and nothing extra: `is_authorized` (hot path),
`is_authorized_batch` (~9× for bulk), `validate_policies` against a
schema (the policy-repo **activation gate**), parsed
`PolicySet`/`Entities` handles to amortize parsing across calls, and
policy templates. It passes the complete official Cedar conformance
corpus (74 integration suites + 59,696 fuzzer cases).

Named risks, accepted with mitigations: it is community-maintained,
**not AWS-official** — no official Python binding exists (AWS ships
Java and Go only) — and it currently bundles engine 4.8.2 while
upstream Cedar is 4.12.0 (2026-07-28), a four-minor lag; every
construct this ADR uses is core-stable since 4.0. We pin the version,
and the documented fallback is permitio/**cedar-agent** as a loopback
HTTP sidecar — noting honestly it is the *weaker* option today
(no commits in ~9.5 months, bundles Cedar **2.5.0**, two majors
behind). Adjacent find for 7.2: AWS's own
`cedar-policy/cedar-for-agents` ships a generator that derives a
Cedar **schema** from an MCP server's tool descriptions (v0.6.0,
active) — a candidate input to the activation gate, not an
authorizer.

**The pattern is proven executable, not assumed:** the four-outcome
ladder (Decision 4) ran on this host on the exact wheel, five cases —
engineering read → `permit`; engineering write staging → `confirm`;
engineering write prod → `forbid` *through* an elevated context (the
forbid override); HR read and HR write on an unassigned server →
`forbid` at every rung (can't even ask). 5/5.

## Decision 2 — The principal: the IdP authenticates, the policy repo authorizes

A person is `Principal::"<email>"`. Two sources combine, with a hard
line between them (P1):

- **Authentication** — Authentik, via the 7.3 gateway OAuth door. The
  broker validates the bearer JWT **itself** (signature against the
  issuer's JWKS, cached; audience + expiry), never trusting a
  gateway-added header — the gateway's own config is in `catalog/` and
  therefore agent-writable. What authentication yields is exactly one
  fact: *this call is being made by this person*.
- **Authorization** — the **policy store** (Decision 5) carries the
  Cedar **entity store**: principals, their group memberships, and the
  group lattice. Token group claims are ignored for Airlock decisions
  — after the scope-mapping finding above, a claim computed by
  agent-PRable code is not evidence.

The group lattice is the owner's model, expressed as Cedar entity
hierarchy (memberships are transitive):

```
Group::"all-employees"                — birthright base, every person
Group::"engineering"                  — role group
Group::"hr"                           — role group
Group::"hr-head" in Group::"hr"       — sub-groups inherit the role's
Group::"hr-recruiter" in Group::"hr"    birthright and add their own
Group::"hr-admin" in Group::"hr"
```

Onboarding a person into Airlock is a **passkey-authenticated console
action** — add the person, pick their groups — audited and versioned
like every other Sentinel decision (Decision 5). (The owner's future
"request access" feature slots here naturally as a workflow that
files *pending drafts* for that console — a later update, not built
now; see Non-goals.)

Principals are keyed by email. On first authenticated contact the
broker pins the IdP `sub` to the principal row (trust-on-first-use);
a later token with the same email and a different `sub` is a named
anomaly, not a silent re-bind — that is the cheap defense against the
IdP re-issuing an address to a new hire.

**The named bound (stated, not hidden):** with Authentik in-cluster,
authentication assurance itself is only as strong as the cluster.
ADR-004 reason 3 (cluster-admin → Authentik-admin → impersonation)
applies to the elevation path: a fully compromised cluster can mint a
valid token *as* a real engineer and borrow what that engineer could
borrow — bounded by that person's policy-repo entitlements, never
wider, and revocable centrally (per-grant revoke, 7.2). The
production-shape fix is an IdP outside the agent's writable domain
(external tenant); for the lab MVP the bound is accepted and named
here. Approvals and the kill switch never ride this path at all
(Decision 6).

## Decision 3 — The resource: declared extraction, evaluated as an entity

`scope.py` keeps deriving the **action** (`<server>.<tool>` — that
part is proven and stays byte-identical). What's new: a per-server
**resource map** in the policy repo declares, per tool, which
argument names the resource and how its **tier** is derived:

```yaml
# policy-repo: resources/postgres-mcp.yaml (illustrative)
tools:
  run_sql:
    resource_from: params.arguments.database   # extraction path
    pattern: "^[a-z][a-z0-9_-]{0,62}$"         # deny-closed validation
    tiers:
      staging: ["app_staging", "scratch_*"]     # explicit membership
      prod:    ["app_prod"]
```

The broker evaluates Cedar against
`Resource::"postgres-mcp/app_prod"` with attributes
`{server, tier, kind}` — and now
`forbid(...) when { resource.tier == "prod" }` is expressible, which
is the flagship example ("`DROP TABLE` forbidden on prod, permitted on
staging") made real.

Rules that keep it honest:

- **Deny closed, like scope.py:** a tool call whose resource map
  exists but fails to extract/validate/classify is denied
  (`unmapped-resource`), not defaulted to a permissive tier. A server
  with *no* resource map gets `Resource::"<server>/*"` with
  `tier: "unclassified"` — and policy decides what unclassified may do
  (for a chat-summarizer, everything; for a database, nothing).
- **The map lives in the policy store, not the chart.** A chart-side
  map would let a PR re-point extraction at a harmless argument and
  smuggle `prod` through as `staging` — the same P1 violation as
  claims. The chart's own tool allowlist (layer 2) stays chart-side;
  it is defense in depth, not the decision.
- **Every resource entity carries a `tier`, always** — explicit or
  `"unclassified"` — enforced by schema validation at the activation
  gate. Not cosmetic: Cedar's third evaluation property is **skip on
  error** — a `forbid` whose condition errors (say, a missing
  attribute) is *silently skipped*, so a data-shape bug in a forbid
  becomes a permit. Attributes that forbid policies reference must be
  total, and the schema is what makes "must" checkable.
- **Audit grows a `resource` column** (and a `principal` column with
  it — 7.2's migration). Every decision row then reads
  *who / which tool / on what / verdict*, which is what
  "recording rich enough to reconstruct an elevation window" requires.

## Decision 4 — The four outcomes are one evaluation, run under three contexts

For a call by principal *P*, action *A*, resource *R*, the broker asks
Cedar up to three times, cheapest first:

1. **Baseline** — `context: {}`. Permit ⇒ **permit** (birthright; log
   and forward — no token dance, no console card, zero approvals).
2. **Elevated** — `context: {elevated: true}` (only reached if 1
   denied). Permit ⇒ the caller *could* do this inside a `confirm`
   window: the broker answers 403 with an `elevation-available`
   hint naming the profile and allowed windows. If the caller holds an
   **active elevation grant** covering *A*, step 2's permit is applied
   directly and the call proceeds (logged against the grant).
3. **Approved** — `context: {approved: true}` (only if 2 denied).
   Permit ⇒ **approve**: possible only via a *different human* on the
   Sentinel console — the rare path, kept deliberately rare.

Deny on all three ⇒ **forbid**. And explicit `forbid(...)` policies
override every permit at every step — Cedar's forbid-trumps-permit is
the reason "no button, ever" is a *property* rather than a convention:
`DROP TABLE` on prod stays forbidden even inside an elevation window,
even with an approval, because a forbid policy admits no context that
disables it.

The owner's two negative properties fall out with no extra machinery:
HR has no birthright on GitHub (no policy permits it at baseline)
**and cannot request it** (nothing permits it under `elevated`
either) — "can't even ask" is just "step 2 denies," and the client-
facing hint never offers elevation that policy wouldn't grant.

Server *visibility* rides the same rails: the MCP handshake scopes
scope.py already emits (`<server>.rpc.initialize`,
`<server>.rpc.tools.list`, `<server>.rpc.transport.*`) become
policy-checked birthright — permitted at baseline for groups the
server is assigned to, denied at baseline *and* under elevation for
everyone else. An HR client cannot even complete `initialize` against
the GitHub server: the server does not exist for them.

## Decision 5 — Where policy lives and how it changes: Sentinel owns it, the console is the pen, git is the memory

*(Amended 2026-08-02 after owner review. The first draft made every
policy change a human-authored PR to a second repo. The owner
rejected the ceremony, rightly: CLAUDE.md promises humans never type
git on this platform, and in a single-admin lab the "second reviewer"
on such PRs is fiction. What the repo was actually buying —
agent-unreachability, history, revert — survives below without it.)*

**Policy is structured data, not hand-written Cedar.** The store is
three small documents under `/var/lib/sentinel/policy/`, owned by the
`sentinel` user:

- the **entity store** — people, groups, the lattice;
- the **access matrix** — per (group, server): `none | read | write |
  write-on-request`, plus allowed windows and tier rules. The
  owner's sketch is the schema, verbatim: *"read only for these
  groups, write for those, or write only via request, and everything
  is audited."*
- the **resource maps** (Decision 3).

**Cedar is generated from that data at activation.** Cedar policy
templates exist for exactly this shape (and `cedarpy` links them). A
raw-Cedar overlay file remains for the rare thing a matrix cannot
say; it passes the same validation gate, and the expectation is that
it stays nearly empty — anything accumulating there is a schema smell
to fold back into the matrix.

**The authoring surface is the Sentinel admin console** — an
**Access screen** (built in 7.2) on the same passkey-gated,
loopback-bound surface as granting and the kill switch. The admin
edits groups, people, and the matrix; **save = validate (schema +
generated policy — the activation gate) → activate atomically → bump
`policy_version` → audit row carrying the diff**. A broken edit never
activates; last-good stays live. Nobody authors a PR; nobody needs
git literacy.

**Git stays — as memory, not ceremony.** Sentinel auto-commits every
activated version to a local repository inside its state directory
(author stamped from the console operator's audit row). That keeps
history, diffs, and revert — "restore version N" is a console action
that re-activates an old snapshot, itself audited — and the directory
rides the same host backup / off-box export path as the audit log
(ADR-004's durability line). An optional push-mirror to a private
remote is a backup knob, never an authoring path; nothing ever
*pulls from* git into the live store.

**Why P1 holds — and holds simpler than the PR design:** the only
writers are passkey-holding humans on the console. The agent cannot
reach the console (loopback + WebAuthn), cannot write
`/var/lib/sentinel` (different user, sandboxed units), and has no
repo to PR because the authoring path contains none. The first draft
required a second GitHub repo whose branch protection had to stay
correct forever; this design deletes that surface entirely.

**Consciously given up, with mitigations:** pre-change review by a
*second* human. Mitigated now by the audit trail + version history;
in Phase 8 by an alert on `policy_version` changes to
`#claude-alerts`; and the named future option (console-addable, no
architecture change) is a **two-passkey confirm** for changes
touching `approve`/`forbid` tiers or admin-grade groups, for when
more than one admin exists.

**Automation drafts, humans activate** — the owner's "build it so
even a small model could modify it" idea, given its safe shape:
because policy is schema'd data, *drafts* can come from anything — a
script, the future access-request workflow, a local 4b model told
"give contractors read on github." A draft becomes live policy only
when a passkey holder confirms it on the console. Proposal/pen
separation, identical to every other flow on this platform: a model
that could *apply* policy would be the self-granting hole reopened; a
model that can only *draft* is free labor.

This still dissolves blockers 3 and 4 **together**: group membership
*is* policy data, so "where policy lives" and "where membership
lives" have one answer — Sentinel's store — and the Authentik
blueprint path stops mattering for authorization entirely (blueprint
groups remain for portal cosmetics and non-Airlock app UX only).

## Decision 6 — The carve: who approves vs. who self-elevates (amends ADR-004)

- **Who may APPROVE (and kill):** unchanged, and deliberately so —
  the small set of humans holding **passkeys registered directly with
  Sentinel**, acting on the loopback admin console. No IdP in that
  path, ever (ADR-004's three reasons stand).
- **Who may SELF-ELEVATE (`confirm`):** any **IdP-authenticated
  person** whose policy-repo entitlements permit the action under
  `elevated` context. The confirm ceremony happens on the caller's own
  authenticated session through the **broker's cluster-facing
  surface** — it never touches the admin listener, needs no passkey,
  and no second human.

ADR-004's sentence "Sentinel behind Authentik SSO: no" was written
about the admin surface and remains true of it; ADR-005 adds the
carve: *the admin surface stays IdP-free; the elevation surface is
IdP-authenticated by design.* ADR-004 gains a pointer to this section
so the two documents agree from day one. The corollary ADR-004
already states — "who may approve is a deliberately smaller set than
who may see" — is now load-bearing architecture, not a footnote.

Elevation mechanics (implemented in 7.2, decided here):

- A `confirm` mints a **windowed grant**: `(principal, profile,
  expires_at)` — a *set* of tools for a *window*, retiring
  one-tool-one-call. **Profiles** are named tool-sets declared in the
  policy store per server at levels (`github:read`, `github:write`);
  the six-approval finding dies structurally, because handshake scopes
  are birthright (Decision 4) and a working session needs **zero**
  approvals at baseline and **one** confirm when it borrows.
- Windows are **policy data, not architecture**: the owner's current
  set is 30 min / 1 h / 2 h (supersedes the 15/30/60 illustration in
  CLAUDE.md), declared per profile — a sensitive profile may offer
  only 30 min.
- Grants are revocable individually (**per-grant / per-flow revoke —
  ADR-004 debt 4 — lands in the same 7.2 schema change**), and the
  global kill switch keeps overriding everything.
- Every elevation is announced to the audit stream at open and close,
  and every call inside it carries the grant id — the reconstruction
  requirement, satisfied by rows that already exist.

## Decision 7 — GitHub MCP server for 7.4

**Upstream: the official `github/github-mcp-server`, self-hosted
in-cluster in its native `http` mode.** Verified 2026-08-02: MIT,
v1.8.0 (2026-07-30), weekly release cadence — and the open-source
binary runs as a real **Streamable HTTP** server (`github-mcp-server
http`, port 8082), with reverse-proxy support whose docs name
"an in-cluster gateway" as the intended deployment, and RFC 9728
protected-resource metadata served at `.well-known` (meshing with
7.3's door). No community fork needed.

- **Layer 2 (server-side restriction), decided:** `GITHUB_TOOLSETS`
  as an explicit allowlist (from the current 21 toolsets; exact list
  chosen in 7.4 — starting shape: `context, repos, issues,
  pull_requests`), `GITHUB_EXCLUDE_TOOLS` for known-dangerous leaves
  (verified highest precedence), `GITHUB_READ_ONLY=1` where a
  deployment should be read-only. Whether birthright traffic gets its
  own `GITHUB_READ_ONLY=1` instance while elevation targets a write
  instance is a 7.4 implementation choice, not architecture — Cedar
  (layer 3) distinguishes read from write either way. Note: older
  writeups describe a "dynamic toolsets" flag; it is **absent from
  current docs** (superseded by scope filtering) — do not design
  around it.
- **Layer 1 (credential):** a **narrowly-scoped fine-grained PAT**,
  SOPS-held. Two verified constraints force this: the server has **no
  repository allowlist of its own** (open upstream issue #1685) — repo
  scoping comes only from the credential — and GitHub App
  server-to-server auth is **stdio-only**, unavailable in `http` mode,
  so the Mission Control App identity cannot extend here.
- **Verify live at 7.4** (probe-the-live-path rule): `--read-only`
  enforcement under `http` mode — a real bypass existed pre-1.0
  (v0.31.0, fixed 2026-04-13); pin ≥ v1.8.0 and test the negative.

## Decision 8 — Slack for 7.5

**Upstream: `korotovsky/slack-mcp-server`, self-hosted in-cluster.**
(MIT, v1.3.0 2026-05-14, actively maintained, published container
image; bind `SLACK_MCP_HOST=0.0.0.0` in-cluster.) The surprise
finding: **Slack ships an official MCP server (`mcp.slack.com`, GA
2026-02-17) — and it is hosted-only SaaS**, no self-host option.
Using it would mean callers reach Slack's endpoint directly,
**bypassing the Sentinel proxy entirely** — so "official" loses to
"gateable." Rejected for that reason (plus marketplace/internal-app
restrictions and unverified plan-tier gating).

- **Layer 1:** an **`xoxb` bot token only**, scopes exactly
  `chat:write, channels:history, channels:read` (+ `groups:*` only
  if a private channel is actually wanted). **Never** the xoxc/xoxd
  "stealth" browser-session mode — verified unreliable (Slack rotates
  those tokens within hours on some plans) and it impersonates a
  human session, which violates the three-layer principle outright.
- **Accepted limitation:** bot tokens cannot use classic message
  search (`search:read` is user-token-only — a verified Slack scope
  asymmetry). Audit/alert posting and channel reads don't need it;
  if search is ever wanted, the path is Slack's newer bot-compatible
  `search:read.public` family, **not** an `xoxp` user token.
- **Layer 2:** the server's write tools are disabled by default and
  enabled per-channel via env allowlists — `#claude-audit` and
  `#claude-alerts` become the only allowlisted write targets.
- **Socket Mode, corrected by verification:** the phase doc's sketch
  assumed Socket Mode was the lab's way in; in fact every tool 7.5
  needs (post, read, list) is an **outbound Web API call** — no
  inbound path of any kind is required, so Socket Mode is
  unnecessary for 7.5's scope. It becomes relevant only if Slack must
  ever *push* to the platform (event subscriptions, buttons, slash
  commands) — none of which the current design wants (Mission Control
  has no Slack; elevation confirm lives on Sentinel's surface).
  Recorded as the trigger condition, not a deliverable.
- **Verify live at 7.5:** the server documents `stdio`/`sse`
  transports; confirm Streamable-HTTP framing against a real client
  before wiring the proxy (generic `sparfenyuk/mcp-proxy` is the
  escape hatch if framing disagrees).

## Decision 9 — XAA / ID-JAG posture (layer 1)

Verified 2026-08-02, primary sources. Where XAA actually is: SEP-990
is **Final**, shipped as the optional MCP extension
`io.modelcontextprotocol/enterprise-managed-authorization`
(stable per the MCP blog, 2026-06-18; current spec revision
2026-07-28). The wire shape: the **enterprise IdP** does RFC 8693
token exchange and issues an ID-JAG
(`urn:ietf:params:oauth:token-type:id-jag`); the **resource-side
authorization server** redeems it via the RFC 7523 `jwt-bearer`
grant. The IETF draft is
`draft-ietf-oauth-identity-assertion-authz-grant-04` (2026-05-21,
OAuth WG, pre-IESG). Shipping IdPs: Okta (its "Cross App Access"
product, plus the zero-setup sandbox at xaa.dev); Microsoft Entra
issues ID-JAGs (integrations in preview); Keycloak has an
experimental *receiver-only* nightly feature flagged not for
production. Anthropic's client side ("Enterprise Managed Auth") is
itself beta and waitlist-gated on Team/Enterprise plans.

**The Authentik verdict:** our pinned 2026.5.6 — which *is* current
upstream as of today — has none of it: no RFC 8693 token exchange, no
`jwt-bearer` *authorization grant* (its JWT-bearer support is client
*authentication*, a different mechanism), no open issue or roadmap
signal for ID-JAG at all. Making in-cluster Authentik the XAA issuer
is not a plan — and P1 independently says the enterprise control
plane must sit outside the agent's writable domain anyway.

**Decision:** the two constraints converge on one future component —
**the XAA-grade IdP and the P1-grade IdP are the same external
tenant**, adopted together when Airlock goes multi-tenant for real.
For Phase 7, layer 1 stays what CLAUDE.md already assumes: **static,
minimally-scoped, per-server upstream credentials, SOPS-held**, with
layer 2 (server allowlist) and layer 3 (Cedar) carrying enforcement.
XAA is the *named upgrade path*, not a 7.x exit criterion: a spike
against xaa.dev validates the shape in an afternoon when wanted, and
building our own ID-JAG issuer against Anthropic's still-beta client
feature would be pioneering both ends of a wire simultaneously —
wrong risk for this phase.

## Homes for the four audit gaps (so nobody rediscovers them expensively)

1. **An elevation's expiry cannot close an already-open SSE stream**
   (ext_authz is per-request). Bounded, not solved: tool *calls* still
   re-check per request, so an expired window cannot execute anything
   new — the stream leaks only server-push notifications. 7.3
   additionally caps MCP route stream lifetime (Envoy
   `max_stream_duration` ≈ the longest elevation window) so no channel
   outlives the entitlement era that opened it. Stated limitation
   until an MCP-aware proxy exists.
2. **Per-tool visibility inside an assigned server** (rewriting the
   `tools/list` *response*) is structurally beyond ext_authz. Accepted
   for MVP: within a server your group is assigned, tool *names* are
   visible even where calls would be denied. Whole-**server**
   invisibility is real today via handshake-scope policy (Decision 4),
   which is the boundary that actually matters (HR never sees GitHub
   at all).
3. **WebAuthn RP ID is `localhost`** — every enrolled passkey dies the
   day the console serves a real domain. Owned by the cloud story:
   re-enrolment is a scripted step in Phase 9's runbook
   (`enroll-operator.sh` already covers the mechanics); not a surprise.
4. **OAuth discovery / client registration for MCP clients** —
   resolved by verification (2026-08-02). The current MCP spec
   (2026-07-28) requires of the *authorization server* OAuth 2.1 +
   PKCE and RFC 8414 / OIDC discovery — Authentik as-pinned has all
   of it (RFC 8414 since authentik 2025.8) — and requires of the *MCP
   server* **RFC 9728 protected-resource metadata**, which is 7.3's
   job at the door, not Authentik's. Client registration: the spec
   now *deprecates* Dynamic Client Registration in favor of Client ID
   Metadata Documents and static registration; Claude custom
   connectors accept a pre-registered static `client_id` (verified in
   Anthropic docs), so Authentik's missing DCR (merged upstream
   2026-07-30 for 2026.8.0, and by its `enterprise/` code path almost
   certainly license-gated) is **not load-bearing — do not chase
   it**. One open verification for 7.3: which registration path
   Claude Code exercises against Authentik (it prefers its own CIMD
   document and falls back to DCR when the AS lacks CIMD support);
   Desktop/web are covered by static registration regardless.

## Consequences

- **7.2 implements:** Cedar evaluation in `check_capability`; the
  console **Access screen** + matrix→Cedar generator + the
  validate/activate path with auto-committed version history; schema
  migration — `principal` on flows/requests/grants, profile grants
  (tool-set + window), per-grant revoke, `resource` + `principal` +
  `policy_version` on `audit_events`. Tenant scoping (ADR-004 debt 1)
  lands **first**, before real person-flows exist.
- **7.3 implements:** the public door + gateway OAuth against
  Authentik, broker-side JWT validation, handshake-scope birthright,
  stream-lifetime caps.
- **7.4 / 7.5** build the GitHub and Slack servers per Decisions 7–8.
- **No new repo.** The `homelab-policy` repo from this ADR's first
  draft is cancelled; the policy store is Sentinel host state with
  its own automatic local git history, and it joins the audit log's
  backup / off-box export line in ADR-004's durability table.
- **CLAUDE.md** needs no structural edit now (the flows section
  already matches); its 15/30/60 illustration is superseded by
  policy-declared windows — correct at the next architecture edit,
  per the no-churn rule.

## Non-goals (decided by the owner, recorded so nobody builds them)

- **A "request access you don't have" workflow** (asking to join a
  group / have a server assigned). Out of scope for the build; the
  natural later shape is a workflow that files pending *drafts* for
  the console's Access screen. Until then: an admin makes the change
  directly on the console.
- **Universal undo** of what happened inside an elevation window.
  Recording is required; reversal is per-upstream-tool and scoped
  honestly where it exists.
- **Per-call human approval at workforce scale.** Measured dead by
  5.5.8; `approve` survives only as the rare, deliberately-heavy path.

## Alternatives considered

- **Trust IdP group claims for authorization.** Rejected — the claims
  are computed by blueprint code the agent can PR (see the threat
  section); this is the quiet version of handing over the keys.
- **Cedar policies in `catalog/`.** Rejected — the operator widens its
  own authorization as a routine-looking values PR (audit blocker 4
  verbatim).
- **A per-MCP-server policy sidecar.** Rejected — a second enforcement
  hop re-implementing what Envoy + ext_authz + broker already do,
  with N config surfaces instead of one.
- **Hand-edited host files with no history.** Rejected — the trail is
  the product; the adopted design gets the trail from audit rows plus
  auto-committed versions instead of human ceremony.
- **A human-authored policy repo with PR review (this ADR's own first
  draft).** Rejected by the owner the same day it was proposed: it
  reintroduces a manual git flow for humans CLAUDE.md promises never
  touch git, and with one admin the second reviewer is fiction. Its
  real properties — agent-unreachability, history, revert — survive
  in the adopted design; its ceremony does not.
- **ToolHive as the Airlock substrate.** Examined at v0.41.0
  (2026-07-28; Apache-2.0, near-daily releases, real Kubernetes
  operator). The strongest *validation* of this ADR available: its
  default authorizer **is Cedar** — via the official `cedar-go` —
  evaluated per `tools/call`, with JWT-group hierarchies and
  tool-annotation resource attributes. And verified by direct source
  and docs inspection to contain **no human-in-the-loop grant, no
  TTL, no elevation window, at any tier** (OSS or Enterprise): a deny
  is a terminal 403. Adopting it would mean swallowing a whole MCP
  runtime/registry/portal platform and *still* building Sentinel's
  actual primitive — the human-granted, expiring, flow-scoped
  capability — ourselves. Rejected as substrate; kept as independent
  confirmation that Cedar-in-front-of-MCP is the converged industry
  pattern. (Its MCP-aware `tools/list` response filtering is also the
  existence proof for gap 2's "different mechanism," if per-tool
  visibility is ever promoted from accepted limitation.)
