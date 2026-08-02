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

**Status:** **7.2 CLOSED 2026-08-02** (7.2.1–7.2.6 all done; live on
the installed units; battery 18/18; owner drove the Access GUI
through two same-day iterations and accepted it as far as example
data allows — v3+ iterates against REAL servers). **7.3 IN
PROGRESS** (opened 2026-08-02, fresh session — checklist below;
kickoff decisions: demo client = **Claude Code in WSL2, exploring
CIMD** with static registration the sanctioned fallback; scope =
local units + cluster + in-cluster Authentik only, wire demo against
the echo stand-in). Then **7.4
deploys the first real MCP server — owner direction: their own
on-prem GitHub as the first upstream** (GHES host; a second
`github-homelab` server entry stays a separate matrix column).
**7.1 DONE 2026-08-02** —
`docs/adr/ADR-005-airlock-policy-model.md` written (**Proposed**;
owner acceptance is the gate to 7.2). All four blockers resolved,
both forward decisions made (7.4 GitHub upstream, 7.5 Slack), all
four audit gaps assigned homes, ADR-004 amended with the carve.
**Amended same day on owner review:** policy authoring is the
Sentinel console's new Access screen (group×server matrix, Cedar
generated, changes audited + auto-versioned) — **not** a human PR
flow; the `homelab-policy` repo idea is cancelled (Decision 5).
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

## Phase 7.2 checklist (capability profiles + multi-user Sentinel)

Scope fixed by ADR-005 (Accepted 2026-08-02) → Consequences.

- [x] **7.2.1 Schema — the person arrives** (DONE 2026-08-02).
  Migration `86c82f996509`, proven up→down→up: `principals` identity
  ledger (email-keyed, TOFU `idp_sub` pin — mismatch refused +
  audited; `disabled_at` offboarding), nullable `principal_id` on
  flows/requests/grants (ADR-004 debt 1 attribution — the "flow-1
  collides" half retired by design: person-flow ids are gateway-minted
  in 7.3), profile grants (`profile` + `tools_json` SNAPSHOT +
  `granted_via` admin|confirm|approve, `flow_id` now nullable with
  flow-less grants DENY-CLOSED on today's path until 7.3's door),
  audit `principal`/`resource`/`policy_version`, per-grant +
  per-flow revoke (ADR-004 debt 4: service + `/v1/grants` list +
  revoke endpoints, 409 on double-revoke). Suite 21/21 (8 new).
- [x] **7.2.2 Policy store + generator** (DONE 2026-08-02).
  `app/policy.py`: three documents (entity store with lattice +
  implicit `all-employees`; five-level access matrix + windows +
  forbids; per-server tool classification with `rpc.*` prefix
  classes + resource maps) → semantic validation (ALL errors at once;
  Cedar-literal **injection guard** — emails/groups/servers restricted
  to a safe charset, rejected not escaped) → generated Cedar
  (deterministic; write* levels imply read; forbids emit engine-level
  `forbid`) → `cedarpy.validate_policies` against a schema where
  `Resource.tier` is REQUIRED (the skip-on-error defense) → atomic
  activate, **last-good-stays-live** → content-hash version →
  auto-commit to the store's own local git (author = the console
  actor). `policy-example/` committed as living documentation AND the
  test fixture (they cannot drift). Admin `GET /v1/policy/status` +
  best-effort startup activation. Dev store gitignored (it grows its
  own `.git`). Suite 33/33 (12 new), crown assert: the ADR's
  four-outcome ladder proven against GENERATED policy — hr-head
  `approve` on staging, `forbid` on prod through every context.
- [x] **7.2.3 The ladder** (DONE 2026-08-02). `app/ladder.py::decide`
  — kill first, no-active-policy denies closed, tool classified via
  the store (`unclassified-tool` = no guessing), resource derived
  with the same reject-don't-escape guard (`unmapped-resource`), then
  the three hypothetical contexts → outcome class, then POSSESSION:
  any live covering grant satisfies `elevated`; only human-issued
  grants (`granted_via` approve|admin — the console door) satisfy
  `approved`; a denial that borrowing would fix carries the offer
  (`profile` + `windows` from the matrix cell via transitive groups).
  `_grant_covers` gained `.*` prefix classes (the handshake rides one
  snapshot entry). Every decision row stamps principal + resource +
  `policy_version`. Proven: birthright permit with zero grants,
  handshake-at-zero-approvals (the six-approvals finding retired in
  miniature), confirm offer→grant→yes→revoke→offer again,
  self-issued grant does NOT satisfy approve, **forbid trumps a held
  human-issued grant on the prod tier**, unassigned server invisible
  even to `initialize`, kill beats policy, store-unknown person =
  forbid regardless of DB. Suite 42/42 (9 new). No HTTP route yet —
  that is 7.3's door, by design.
- [x] **7.2.4 Console Access screen** (DONE 2026-08-02). New
  `policy_change` audit type (migration `f41d02a9c8b3` — fourth
  hand-widened CHECK; rejections audited on purpose: a stream of them
  is somebody probing the policy surface). Store API on the admin
  listener: GET store (editor texts from disk + parsed view), **PUT
  save-and-activate with the load-bearing property that a rejected
  save never touches disk** (candidate validated in a throwaway dir
  first — tested), GET history (the store's git), POST revert
  (forward restore; content-hash brings the SAME version id back as a
  NEW commit — tested: 3 commits, nothing rewritten). Console UI:
  rendered matrix grid + people list, four textarea editors with
  every-error display, Save & Activate / Reload, history rows with
  Restore buttons, and a "Borrowed right now" panel with per-grant
  **Revoke** buttons; editors never repopulate from the poll (unsaved
  edits are the operator's). Suite 45/45; JS syntax verified (node in
  a throwaway container — no node on host). **Deferred as polish:** a
  clickable cell editor (dropdown grid) — textareas + rendered grid +
  full validation loop is the MVP; **owner has not yet SEEN the
  screen** — browser look rides 7.2.5's install (same note 5.5.5
  carried).
- [x] **7.2.5 Proof battery + live install — builder half DONE
  2026-08-02; owner half = one line + one look.** Installer gains:
  the policy store (**seed-once** from `policy-example/`, never
  re-seeded — the store belongs to the operator from the moment it
  exists; `SENTINEL_POLICY_DIR` in the env file), a **hard git
  requirement** (activation dies without git — refuse loudly at
  install, not quietly at runtime), a **pre-migration DB snapshot**
  (same-script backup, keep last 3 — the hosts-file lesson as
  policy), and re-install awareness (**operators already enrolled →
  "log in as usual" banner**, no enrollment code minted at an
  enrolled human). Smoke battery now asserts the policy plane first
  (status ACTIVE + grants surface answering). **Honesty
  adjustment:** the END-TO-END one-approval-per-session demo needs
  the `confirm` door, which is 7.3 — it moves to 7.3's proof; the
  retirement is proven at unit level today (test_ladder's
  handshake-birthright). Owner half: run the install line, log in as
  usual, look at the Access screen.

- [x] **7.2.6 — the Access GUI proper** (owner-promoted 2026-08-02 on
  first look — *"an actual GUI… Groups, permissions, tools"*, the
  second friction veto of the day after the PR flow — **BUILT same
  day**; owner look pending deploy). The matrix is **clickable** — a
  level dropdown per group×server cell (`none` removes the entry,
  empty group rows are pruned); people are rows with **group
  checkboxes** + add/remove; groups get a parent dropdown
  (`all-employees` is uneditable — it is load-bearing); **forbids
  read as a sentence** ("no *write* on *hr-platform* tier *prod*")
  built from dropdowns; servers edit their read/write tool classes
  in place (a new server starts with `rpc.*` read so it can at least
  handshake). Saves go through a new **structured endpoint** — JSON
  in, server-side YAML emission (deterministic: unchanged intent =
  unchanged version, tested), the SAME validate→activate gate, and
  the **overlay is preserved from disk** so a GUI save can never
  blank the escape hatch (tested). The raw documents live on in the
  **Advanced drawer** (owner's ask verbatim: "see/edit the store
  with advanced settings… keeping your layer underneath"), labeled
  honestly that GUI saves rewrite them without comments. Draft never
  clobbered by the poll; Reset-to-active button. Suite 46/46; JS
  node-checked. Residual: tiers/resource-map editing stays
  Advanced-only for now (rarely touched; a guided form is future
  polish). **v2 same day, from the owner's review of v1** ("I can't
  easily see who has access to what"): rebuilt as four lenses on the
  established IAM patterns (GitHub access page, Okta assignments,
  Tailscale/Access-Analyzer resolved views) — **Groups** (click →
  members, grants, inherited-via chips), **People** (search, capped
  20, click → EFFECTIVE access with provenance — whichever-is-higher,
  computed and shown), **Servers** (who-can-reach-it buckets;
  "environments" replaces tier/map jargon), **Limits & windows**
  ("Never allow" sentences + window chips). Human level labels
  throughout. Same draft→structured-save gate underneath.

## Phase 7.3 checklist (the public MCP door + gateway OAuth)

Scope fixed at kickoff (owner, 2026-08-02): local units + cluster +
in-cluster Authentik only — no external SaaS, no real upstreams
(that is 7.4); the wire demo runs against the in-cluster stand-in
(echo). Demo client: **Claude Code in WSL2**, exploring **CIMD**
client identity, static registration the sanctioned fallback
(ADR-005 D9; DCR stays dead). Auth posture per the 7.2 note: OAuth
2.1 + PKCE only, discovery via RFC 8414/OIDC, no legacy flows, no
RFC 8693 at the gateway.

- [x] **7.3.1 Broker-process policy activation + reload** (DONE
  2026-08-02, code + tests; live rollout rides 7.3.6). The
  load-bearing 7.2.5 note: the console activates policy in the
  ADMIN process only; the broker has never loaded the store. Give
  the broker best-effort startup activation (inactive/broken store =
  person path denies closed, same as admin) plus a reload path so a
  console activation reaches the broker without a restart —
  mechanism decided in-item (mtime/HEAD watch vs SIGHUP vs
  admin→broker poke), with the invariant stated and tested: **the
  two processes must never disagree about the active version**
  beyond a bounded convergence window; every decision row already
  stamps `policy_version`, so disagreement is observable, and a
  broken candidate keeps last-good serving in BOTH processes. Code
  + tests only; rollout rides 7.3.6's install.
- [ ] **7.3.2 The Authentik client for the door.** Static OAuth 2.1
  + PKCE client (auth-code flow) in Authentik via blueprint, and the
  CIMD exploration: observe what Claude Code ACTUALLY sends against
  an OAuth-guarded MCP endpoint (URL-shaped client_id? metadata
  document fetch?), verify whether Authentik 2026.5.6 can consume a
  URL client_id — statically registered with the CIMD URL as the
  client_id if native fetch is absent — and record the finding
  honestly (CIMD-shaped vs full CIMD). RFC 8414/OIDC discovery
  verified against the live Authentik; the redirect-URI story for a
  loopback CLI client settled here.
- [ ] **7.3.3 The door itself.** `mcp.lab.local` (local equivalent
  of `mcp.<domain>`) with real TLS from the lab CA. Topology decided
  in-item against CLAUDE.md's "on Envoy" line — which listener
  terminates it (existing Traefik 8443 entry vs the Envoy AI
  gateway), recorded with reasoning. RFC 9728 protected-resource
  metadata served at the door so MCP clients discover the
  authorization server; broker-side JWT validation (issuer,
  audience, expiry, signature against Authentik's JWKS) so every
  call knows WHICH PERSON is calling; person-flow ids
  GATEWAY-MINTED (7.2.1 note — unique by construction, never
  client-chosen).
- [ ] **7.3.4 Handshake birthright through the ladder.** The door
  routes person-calls into `ladder.decide()`: a signed-in person
  reaches their assigned servers' handshake (`rpc.*`) and
  birthright tools at ZERO approvals; unassigned servers invisible
  even to `initialize`; store-unknown person = forbid. Stream-
  lifetime caps: a long-lived MCP stream cannot outlive policy —
  re-evaluation cadence/cap decided and tested here.
- [ ] **7.3.5 The confirm + approve doors.** A borrowable denial
  already carries the offer (7.2.3); the doors mint the profile
  grants: `confirm` = the caller self-elevates from their own
  client, time-boxed, `granted_via=confirm`; `approve` = the
  request lands as a console card and a passkey holder grants
  (`granted_via=approve`, `granted_by` = the approver; lab collapse
  stays named — one enrolled human is requester≈approver until a
  second passkey enrolls). THE MEASUREMENT: one honest MCP session
  end-to-end costs at most ONE approval — the 5.5.8
  six/seven-approvals headline retired on the wire, not just at
  unit level.
- [ ] **7.3.6 Install line + battery + demo + close.** Install
  (restarts + wire probes; carries the two console fixes from
  `96f9d19`); battery extended to assert the door (discovery
  documents served, an unauthenticated call refused, a birthright
  call passing, the one-approval measurement); SETUP.md gains the
  door section (+ the pending certutil wording fix from backlog).
  The demo: owner's real Authentik identity enrolled as a principal
  via the Access GUI, Claude Code pointed at `mcp.lab.local`,
  sign-in, birthright tool call at zero approvals, one confirm
  elevation — captured as the demo asset. STATUS + phase-doc
  close-out.

## ADR-005 must resolve — blockers from the 2026-07-28 audit

Recorded before the re-cut as five. **Original #1 — "no identity can
approve the agent's PRs" — DISSOLVED on 2026-07-28:** the approver
is a human with their own GitHub account, and that is the feature
(GitHub 422s self-approval unconditionally, so "the bot merges on
✅" was never implementable). Its Phase 6 residue, the admin-bypass
decision, is phase-06 item 6.4. Four survived, and **all four are
resolved by ADR-005 (2026-08-02):** 1 → Decision 3 (resource maps in
the policy store, deny-closed, tier always total); 2 → Decision 6
(the carve; ADR-004 amended in place); 3 + 4 → Decision 5 with one
answer (the agent-unreachable **policy store** — Sentinel-owned,
authored from the console's Access screen, Cedar generated from the
access matrix, auto-versioned — carries the policies *and* the
entity store: group membership IS policy data). Kept below for the
record:

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
- **2026-08-02 (7.1 amendment, owner review):** the first-draft
  "policy changes are PRs to a second repo" flow broke CLAUDE.md's
  "owner never types git" promise — owner caught it on first read.
  Decision 5 rewritten: the Sentinel console gains an **Access
  screen** (7.2); policy is a group×server matrix (`none / read /
  write / write-on-request` + windows + tier rules) from which Cedar
  is *generated*; every change is passkey-gated, audited, and
  auto-committed to a local history (revert = console action). The
  owner's automation idea gets its safe shape: anything may *draft*
  a policy change; only a passkey holder *activates* it.
- **2026-08-02 (7.1 second refinement, owner):** the matrix gains a
  fifth level — `write-on-approval` (high risk, a *different human*
  decides) — and it costs almost nothing to build: it is 5.5.5's
  pending-card + grant flow reused as Airlock's `approve` path (same
  windowed-grant primitive, other door, `granted_by` = the approver).
  Generator mapping stated in Decision 5; lab collapse (one human =
  requester ≈ approver until a second passkey enrolls) named
  honestly in Decision 6.
- **2026-08-02 (7.2.1):** ADR-004 debt 1's "two users' flow-1
  collide" is retired WITHOUT re-keying `flows`: under accepted
  ADR-005, person-grants hang on the PRINCIPAL (profile × window),
  and 7.3's gateway mints person-flow ids itself (unique by
  construction) — client-chosen ids only ever existed in the
  single-tenant mTLS domain. Attribution columns are what had to
  land before rows accumulate, and did.
- **2026-08-02 (7.2.2):** two patterns worth keeping: (1) the
  committed `policy-example/` store IS the test fixture (`_mk_store`
  seeds from it) — documentation that drifts from behavior fails CI;
  (2) names that land inside Cedar string literals are REJECTED on a
  safe charset, never escaped — escaping is where injection bugs
  live. Also: `profile_tools` may return `rpc.*` prefix entries in a
  grant snapshot, so `_grant_covers` needs prefix awareness when
  7.2.3 wires profiles to the checker (recorded so it isn't missed).
- **2026-08-02 (owner question, noted for 7.3 — auth compatibility
  posture):** the door speaks what MCP clients speak and nothing
  more: **OAuth 2.1 + PKCE** (auth-code flow) via Authentik,
  discovery via RFC 8414/OIDC, client registration static or CIMD
  (DCR deprecated upstream — not chased). **Legacy OAuth 2.0 flows
  (implicit, password) are deliberately unsupported** — OAuth 2.1
  removed them, no MCP client needs them, supporting them is pure
  attack surface. **"STS"/token exchange (RFC 8693) is exactly the
  ID-JAG/XAA wire** — ADR-005 D9: not built in Phase 7, arrives with
  the external-IdP move; cloud-specific STS (AWS AssumeRole etc.)
  would be a layer-1 upstream-credential concern of a specific MCP
  server, never a gateway feature. Any MCP server slots in via
  catalog chart + `servers.yaml` classification, each bringing its
  own layer-1 credential shape (PAT, xoxb, OAuth, …).
- **2026-08-02 (7.2.5, load-bearing for 7.3):** the console's PUT
  activates policy in the ADMIN process only; the broker process
  never loads the store today (nothing there consumes it — the
  ladder has no route yet). When 7.3 wires `decide()` into the
  broker's door, the broker needs its own startup activation PLUS a
  reload path (store-mtime watch, SIGHUP, or an admin→broker poke) —
  two processes must never disagree about the active version.
- **2026-08-02 (7.2.1):** deliberately deferred: an elevation-CLOSE
  audit event at window expiry (open is the GRANT row; expiry is
  lazy, nothing sweeps). Decide at 7.2.4 whether the console's
  version of "closed" (display) is enough or a lazy close-event
  emitter is wanted. Also: `granted_via` values admin|confirm|approve
  exist in schema now; the confirm/approve DOORS are 7.3.
- **2026-08-02 (7.3.1):** mechanism decision — a store-signature
  WATCH in both processes (stat of the four source docs per tick,
  default 2s via `SENTINEL_POLICY_RELOAD_SECONDS`, 0 disables), not
  SIGHUP (no privilege path between the units) and not an
  admin→broker poke (new surface on the mTLS listener, and a poke
  missed while the broker is down needs a catch-up scan anyway —
  which IS the watch). The broker's path is **read-only**:
  `policy.refresh()` rebuilds and swaps in memory, writes no
  `generated/`, commits nothing — the console remains the store's
  only author, so git history attribution stays human. Torn reads
  (a save caught between its four file writes) are excluded by a
  before/after signature stability check (`StoreUnstable` = skipped
  tick, retried); the same-process thread race (console save in the
  threadpool vs watcher in the event loop) is closed by a
  `loaded_at` guard in the swap. Broker `/healthz` now reports
  `policy_version` (null = the deny-closed state, visible not
  silent) — 7.3.6's battery asserts admin == broker on the live
  wire. Python trap from the code map fixed in `main.py`: the
  lifespan called `activate()` through a default argument frozen at
  import; it now passes `policy.POLICY_DIR` at call time. Deploy
  delta: none (env default applies, unit files unchanged). Suite
  52/52 (6 new in `tests/test_policy_reload.py`).
