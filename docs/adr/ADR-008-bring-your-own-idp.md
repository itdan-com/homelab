# ADR-008: Bring-your-own-IdP — Okta/Ping/Entra compatibility without giving up the shipped default

**Status:** **ACCEPTED** (owner, 2026-08-22: *"accept 7.8 i guess?
… gotta get started on some of this after we land"*). Binding from
this point forward, same as every other accepted ADR in this repo.
Build order is 7.8.1 → 7.8.2 → 7.8.3, scheduled after Phase 8's two
remaining items (ADR-006 implementation; Alertmanager's Slack
receiver) — the owner can pull it forward.
Written from an eight-agent research pass (four readers over the door,
ADR-005, the Authentik chart, and the ADR-007 landscape record; four
web researchers over Authentik federation, the enterprise-SSO product
bar, the MCP identity ecosystem, and Okta/Entra/Ping specifics),
triggered by the owner's question: *"a way to connect to Okta, Ping
Identity or other key identity players without just trying to be our
own one stop shop? what would that take?"*

**Relationship to ADR-005 Decision 9:** this ADR does not reverse the
2026-08-02 amendment ("the shipped IdP is the end state"). Authentik
still ships as the batteries-included default — "domain in, platform
out" must keep working for a company that has no IdP at all. What this
ADR does is **schedule and design the path D9 already named**:
*"bring-your-own-IdP is the enterprise upgrade path, not the plan of
record."* The market moved (see Context), and the upgrade path is now
worth a plan of its own. One retiming, owned explicitly: D9 tied
external-tenant adoption to "when Airlock goes multi-tenant for real";
this ADR deliberately advances that timing to single-tenant
deployments, on the owner's trigger and the market findings below.

## Context

Three facts, one from our own code and two from the field, make this
cheap and timely rather than speculative.

**1. The door is already ~90% IdP-agnostic, by accident of P1.**
The Airlock door (`sentinel/app/door.py`) consumes exactly three
*identity* claims from the IdP: `email`, `sub`, `name`
(door.py:445-453; the rest — `exp`/`iat`/`iss`/`aud` — are
validation-only). No
groups, no roles, no scopes carry authority — ADR-005 P1 means WHAT a
person may do comes only from Sentinel's console-owned policy store,
never from a token. The protocol leg is generic OIDC: live discovery
off a configurable issuer (`SENTINEL_OIDC_ISSUER`, config.py:160-173),
auth-code + PKCE S256, JWKS-validated RS256 id_tokens. The test suite
already treats the IdP as a generic stub — nothing Authentik-shaped is
asserted (tests/test_door.py:211-244). The industry's usual BYO-IdP
pain — untangling group-claims-drive-permissions — does not exist
here because we never tangled it. (Contrast ToolHive, whose default
Cedar authorizer keys on JWT group hierarchies — the exact input P1
refuses.)

**2. BYO-OIDC is now table stakes among MCP gateways.** ToolHive
fronts MCP servers with any OIDC-compliant IdP; agentgateway validates
against any configured issuer + JWKS; Obot (enterprise-tier-gated) and
MintMCP sell Okta/Entra integrations; Cloudflare Access brokers
existing IdPs into MCP Server Portals. (All vendor-stated capability
claims, unprobed — per this repo's rule, several may be partial or
tier-gated.) Authentik-only is the one axis where
this platform is behind the field it otherwise leads (nobody listed
above has human-in-the-loop elevation, TTL grants, a kill switch, or
tamper-evident audit — verified for ToolHive by source inspection in
ADR-005's research, claimed-not-probed for the rest).

**3. The enterprise standard landed, and it is IdP-shaped.** The MCP
spec's **Enterprise-Managed Authorization** extension
(`io.modelcontextprotocol/enterprise-managed-authorization`, SEP-990)
went stable 2026-06-18: the user SSO-signs-into their MCP client at
the *enterprise IdP* once; the client silently exchanges the ID token
for an **ID-JAG** (RFC 8693 token exchange); and presents it to the
MCP server's authorization server via RFC 7523 — zero per-server
consent screens, zero browser redirects. Okta ships the issuing side
(Cross App Access); Anthropic's clients (Claude, Claude Code) and VS
Code consume it (Anthropic's side recorded as beta/waitlist-gated on
Team/Enterprise plans as of ADR-005 D9's 2026-08-02 verification —
re-verify, see probes); Auth0 issuance is rolling out
(vendor-announced Aug 2026); Ping is the strongest RFC 8693
implementer of the big three (though its ID-JAG issuance, like
Entra's, is roadmap, not shipped); Entra has **no** RFC 8693 at all
per community reporting (only its proprietary On-Behalf-Of variant of
RFC 7523) — unverified against primary Microsoft docs, on the probe
list. Our door is already its own
authorization server — exactly the component that would accept an
ID-JAG. ADR-005 D9 anticipated this convergence: *"the XAA-grade IdP
and the P1-grade IdP are the same external tenant."*

**The security upside is already written down.** ADR-005 Decision 2's
named bound: with Authentik in-cluster, authentication assurance is
only as strong as the cluster — a fully compromised cluster can mint
a token *as* a real engineer. The stated fix is "an IdP outside the
agent's writable domain (external tenant)." A customer's Okta/Ping/
Entra **is** that fix. This ADR is therefore a security upgrade and a
compatibility feature in one motion.

## What does not change, no matter what

- **P1 stands, even with an external IdP.** Group claims from Okta or
  Entra still do not drive Cedar decisions. The premise that made P1
  necessary (agent-PRable blueprints computing claims) weakens with an
  external IdP, but the conclusion survives on independent grounds:
  console-owned entitlements are what make offboarding, audit, and the
  elevation ladder coherent, and they are the moat (see Alternatives).
  The external IdP proves WHO; Sentinel's store decides WHAT — same
  sentence as today, stronger assurance behind the WHO.
- **Approvers and the kill switch stay local Sentinel passkeys.**
  The authority is ADR-005 D6 ("No IdP in that path, ever") and
  ADR-004's 2026-08-02 amendment. ADR-004:282-285 notes an external
  IdP would not suffer its reasons 1 and 3 and calls an admin-path
  IdP "defensible as a convenience later" — this ADR deliberately
  hardens that allowance into **never**: reason 2 (failure domain)
  survives any IdP swap — a third-party IdP outage must never lock
  the kill switch — and the carve (approvers ≠ self-elevators) is
  load-bearing. Recorded here so the override is explicit, not
  smuggled.
- **Authentik ships as the default.** A deployment with no external
  IdP configured behaves exactly as today. BYO-IdP is configuration,
  not a fork.

## Decision 1 — One enterprise IdP per deployment; email stays the person key; the pin becomes (issuer, sub)

A deployment trusts **exactly one upstream issuer at a time** (the
shipped Authentik by default, or one external tenant). Multi-IdP
simultaneous sign-in is explicitly out of scope (see Alternatives).

Why this boundary is load-bearing: the policy store and the principal
ledger key people by **email** (models.py:44-67, policy.py:218-232).
With one IdP, email is that IdP's managed namespace and the TOFU
`sub`-pin catches re-issued addresses — defensible. With two IdPs
live at once, any IdP can assert any email, and email-joining becomes
the documented account-takeover class (Vaultwarden GHSA-6x5c-84vm-5j56;
Microsoft's nOAuth guidance; NIST SP 800-63C treats email as an
attribute, never an identifier). Rather than redesign the person key,
we keep it and enforce the boundary that keeps it safe.

Concretely:

- `idp_sub` becomes a **(issuer, sub) composite** — today it is a
  globally-unique bare string with no issuer qualifier (models.py:64,
  migration 86c82f996509). Same TOFU semantics, issuer-qualified.
- Store the vendor-stable ID where the id_token carries one (Entra:
  `oid`+`tid` — its `sub` is pairwise per app registration
  [verified], and community-reported to change if the app
  registration is recreated [probe it]; Okta/Ping: their user ids)
  as an optional **recovery attribute**, never a join key.
- An **audited operator migration action** for changing the deployment
  IdP (Authentik → customer Okta): today an IdP swap trips
  `idp-sub-mismatch` for every principal with no supported re-pin path
  (service.py:538-547) — permanent lockout by design, which is correct
  against a rogue token and wrong against a planned migration. The
  console gets a declared-migration mode: operator opens it with a
  passkey, each principal's first sign-in from the NEW issuer re-pins
  with a `principal-sub-repin` audit row naming old and new issuer,
  mode auto-closes. Outside that mode, mismatch keeps refusing.
  Stated honestly: the migration mode IS a deliberate,
  operator-declared, audited email-join — the one context where the
  join this ADR otherwise condemns is acceptable, because a single
  passkey holder has attested the new issuer, only one issuer is live
  at a time, and every re-pin is individually recorded.

## Decision 2 — Genericize the door (the wart list is the work list)

The door's Authentik coupling is thin; these are the concrete items
that make "point it at Okta/Entra/Ping" true rather than aspirational.
All are small; together they are roughly one session with tests.

1. **Installer preflight** (install-systemd.sh:394) reconstructs
   Authentik's `/application/o/<slug>/` URL shape and hard-fails on
   any other issuer. Fix: probe the issuer's discovery URL the way
   the door itself does (door.py:165-176) — transport-rewritten with
   the Host header pinned when `OIDC_HTTP_BASE` is set, the raw
   `${OIDC_ISSUER%/}/.well-known/openid-configuration` otherwise — so
   the lab install does not regress while external issuers pass.
2. **Email claim mapping.** The door hard-requires `email` in the
   id_token (door.py:445-447). Entra commonly omits it unless the
   optional claim is configured. Add `SENTINEL_OIDC_EMAIL_CLAIM`
   (default `email`, e.g. `preferred_username`/`upn`) and a userinfo
   fallback for IdPs that put identity there. Keep the hard refusal
   when the mapped claim is absent — a person without a stable email
   cannot exist in the policy store.
3. **Host-header pinning** (door.py:172, 187, 426) forces
   `Host: <issuer netloc>` on every IdP call, unconditionally. Correct
   only for the lab's split-horizon wart; breaks IdPs whose token/JWKS
   endpoints live on a different host (custom-domain tenants). Make it
   conditional on `OIDC_HTTP_BASE` being set — the wart stays
   honestly lab-scoped (config.py:165-173 already names it that).
4. **Add `nonce`** to the authorize leg and validate it on the
   id_token (door.py:394-400 sends none today). OIDC Core grounding,
   not vendor lore: PKCE binds the code leg, `nonce` binds the
   id_token to the session, and `azp` is required when `aud` is
   multi-valued — validate it then.
5. **Confidential-client provisioning.** `SENTINEL_OIDC_CLIENT_SECRET`
   exists in config (config.py:163) but no installer ever writes it,
   and only `client_secret_post` is spoken (door.py:418-422). Add
   `client_secret_basic`, and an installer path that accepts a secret
   without echoing it into logs. (Vendors are believed to default to
   basic — unverified this session; the live test matrix confirms
   each vendor's token-endpoint auth method.)
6. **Issuer validation stays exact-match** (door.py:435) — correct,
   and it means Entra deployments MUST use the tenant-specific issuer
   (`login.microsoftonline.com/<tenant-id>/v2.0`), never `/common`
   (whose discovery document carries a literal `{tenantid}`
   placeholder as the issuer). A docs note, not a code change.
7. **RS256-only** key handling (door.py:192, 434) is expected to be
   fine for all three target vendors (unverified — the live test
   matrix confirms it implicitly); note it as a known bound, do not
   speculatively add EC/EdDSA.
8. **Per-vendor registration docs**: "register this app in
   Okta / Entra / Ping" pages with the exact redirect URI, claims, and
   the vendor traps (Okta: the free **org authorization server**
   suffices for our flow — id_tokens are what we validate; the paid
   API Access Management tier is only needed for custom-AS features we
   do not use. Entra: tenant-specific issuer, `oid`+`tid`, optional
   email claim. Ping: per-environment issuer path — fine, since we
   discover from the full issuer URL; which claims PingOne's id_token
   actually carries for our app is a probe item, not assumed).
9. **Cleanups while in there:** the stale blueprint comment claiming
   "the broker validates door tokens against exactly this key"
   (oidc-blueprints.yaml:348-350 — no such validation exists), and the
   `door_session` cookie's `path='/elevate'` (door.py:469), which the
   `/link/{server}` page can never receive (door.py:1037-1045) — so
   account-linking is an **infinite redirect loop today**: the session
   check can never pass, and every pass bounces back to the IdP. A
   real pre-existing bug this work trips over anyway; fixing it is in
   scope.

Acceptance is a **live test matrix**: the door completes sign-in,
TOFU pin, elevation, and token mint against a real Okta dev org, a
real Entra tenant, and a PingOne trial — not against docs (the
verify-the-vendor rule; GitHub's http-mode token fallback that did not
exist is this repo's own scar).

## Decision 3 — Offboarding posture in external-IdP mode

The honest current state: fired-at-Okta does not disable the Sentinel
principal. The door's own token lives `DOOR_TOKEN_TTL_MINUTES` = 480
(config.py:180-181). The *ledger* is the designed kill point —
`person_from_bearer` re-reads the principal row per call and refuses
disabled principals (door.py:531-550) — but the adversarial review of
this draft found that **`disabled_at` has no writer anywhere in the
codebase** (declared at models.py:67, read in two places, set by
nothing): there is no console disable-principal action today, so the
kill point exists as a check with no trigger. JIT-style login
federation structurally cannot deprovision either: no login happens
after termination, so no signal arrives. The industry answer is SCIM
push or aggressive TTLs.

- **A console disable-principal action** (passkey-gated, audited,
  reversible) joins 7.8.1's work list as a prerequisite — the
  "manual console offboarding" this decision relies on must exist
  before the posture below is honest.

- **External-IdP mode defaults the door token TTL down** (60 min,
  configurable) — the named tradeoff is re-auth frequency, and the
  15-minute elevation session (door.py:881) already re-federates to
  the IdP often, so elevations stay IdP-fresh.
- **ADR-007 Decision 4's reconciliation gains a check (d):**
  principals enabled in Sentinel whose IdP account is disabled or
  deleted. Requires a read credential against the IdP's directory API
  (Graph / Okta API / PingOne API) — optional, config-gated,
  read-only, and itself a **tier-3 shared credential under ADR-005
  D10's ladder**, subject to the stated rotation interval ADR-007 D4's
  addendum imposes. One deliberate divergence from D4's output path:
  D4 routes findings through the Mission Control PR gate because its
  fixes are declarative matrix edits — but check (d)'s remediation
  (disabling a principal) is a console act on the entity store, which
  has no PR path by design (P1) and must never gain one. Check (d)
  therefore surfaces on the Sentinel console and the alert stream,
  never as a PR. A deployment without the read credential relies on
  TTL + the console disable action, and the docs say so plainly.
- **Inbound SCIM is the named future, not this ADR's work.** Two
  viable shapes when wanted: Authentik's SCIM *source* in broker mode
  (exists today; its token is a tenant-wide provisioning credential —
  treat accordingly), or a Sentinel-native SCIM endpoint (real work,
  real enterprise checkbox, later).

## Decision 4 — Authentik broker mode for the rest of the platform

Airlock's door is only one SSO consumer; OpenWebUI, Grafana, and
ArgoCD are Authentik OIDC clients and stay that way. For a customer
who wants their Okta behind the *whole platform*, the answer is not
per-app rewiring — it is **Authentik as a federation broker**: the
customer's IdP plugs in as an Authentik **Source** (dedicated Entra
and Okta source types exist, plus generic OIDC/SAML), every consumer
chart is untouched, and Authentik mints its own stable downstream
`sub` regardless of which source authenticated.

Constraints from the research, binding on the implementation:

- **Identifier matching mode only** (pin on the source's `sub` /
  SAML NameID). Authentik's own email-link and username-link modes
  carry its own documented takeover warning; they stay off.
- **Upstream logout is believed not to propagate** (open upstream
  issue) — an Authentik session can outlive the Okta session.
  Confirm empirically on the deployed build (probe list), then
  compensate with session-lifetime settings and say so in docs.
- **Upstream MFA is implicitly trusted**: the stock source-
  authentication flow blueprint runs no local MFA stage (verified in
  the upstream blueprint), and community reports say no acr/amr is
  forwarded downstream. Confirm the deployed flow is the stock one
  (probe list). Acceptable posture, but a documented one.
- **Broker mode does not fix Decision 2's named bound.** Authentik
  stays in-cluster and agent-PRable; the assurance ceiling for
  Airlock only rises when the DOOR points at the external IdP
  directly (Decision 2 of this ADR). The two compose: door → customer
  IdP directly, portal apps → Authentik brokering the same IdP.

Deliverable: a documented, blueprint-shaped Source configuration per
vendor plus the probe results (below) — mostly docs and config, not
code.

## Decision 5 — The flagship: an EMA/ID-JAG receiver at the door

The door's AS facade grows the **receiving half** of Enterprise-
Managed Authorization: accept `urn:ietf:params:oauth:grant-type:jwt-bearer`
at the token endpoint with an ID-JAG asserted by the deployment's
configured enterprise IdP; validate signature (IdP JWKS), `iss`,
`aud` (us), `exp`; map `sub`/email onto the principal ledger through
the same TOFU pin; mint the door's own resource-bound person-token
exactly as the interactive flow does (door.py:509-522). The Cedar
ladder, elevation doors, grants, and audit are untouched — EMA
replaces only the interactive browser leg, which is precisely how the
door was factored (ADR-005 D9: "the facade federates human login…"
— the upstream leg was always the swap point).

Why this is the differentiator and not a checkbox: for an enrolled
enterprise this is **zero-touch SSO into Airlock** — sign into Claude
with Okta once, birthright tools just appear, elevation still runs
through our ladder. No self-hosted open-source gateway ships this
today except agentgateway's enterprise tier (vendor claim, unprobed);
no open-source IdP issues ID-JAGs in production (Keycloak's receiver
is experimental in nightlies; its issuer side is not yet implemented
at all). We would be the self-hosted gateway that
speaks the enterprise agent-auth standard while keeping the
human-in-the-loop layer nobody else has.

**EMA is additive, never a replacement — a hard rule, not styling.**
The interactive browser leg SURVIVES alongside it: `/elevate` and
`/link` ceremonies exist precisely so a human, not a model, performs
them behind the company IdP (door.py's own design comment), and the
`door_session` that admits a person to those pages is minted only by
the interactive callback flow (door.py:466-470). **No EMA token
exchange may ever mint a `door_session` or satisfy a confirm/approve
ceremony** — a silently machine-exchanged assertion satisfying a
human-confirm rung is exactly the hole the elevation doors exist to
close (ADR-005 D6; a machine-issued token must never satisfy a
human-approval rung). EMA replaces the *sign-in* leg for API access;
the ceremonies keep their browser.

Sequencing gate, honestly stated: EMA stabilized against spec revision
2025-11-25, and 2026-07-28 removed sessions and moved capability
declaration to per-request `_meta` — check the ext-auth repo for a
revision aligned to the new core, and verify whether Anthropic's
client accepts an arbitrary ID-JAG issuer or is Okta-wired today,
BEFORE building. The spike is cheap and already priced: ADR-005 D9
said "a spike against xaa.dev validates the shape in an afternoon."
XAA also explicitly requires an active human session — it is for the
workforce flow, not for autonomous agents, which keeps it cleanly
inside Airlock and away from Mission Control.

## Decision 6 — SAML: never at the door; broker mode is the answer

Community consensus (Clerk, WorkOS — no defensible public number
exists) is that security reviews at larger companies still routinely
demand SAML, but all three target vendors speak OIDC natively, and the door
speaks MCP's OAuth 2.1 dialect to clients regardless. Building SAML
into the door buys nothing an Authentik SAML Source (Decision 4)
does not already provide for the stragglers. OIDC-only at the door,
SAML via broker, stated plainly in the docs.

## Build order and sizing

Named **Phase 7.8 — Airlock: external identity** (7.6 audit
durability and 7.7 per-user GitHub credentials are taken). Scheduled
AFTER Phase 8's two remaining items (ADR-006 implementation stays the
next action; this ADR waits its turn — nothing here blocks
observability work).

1. **7.8.1** Door genericization + identity ledger (Decisions 1-2):
   the wart list, the composite pin, the migration mode, and the
   console disable-principal action (Decision 3's prerequisite —
   `disabled_at` finally gets a writer). One session.
   Exit: the live test matrix passes against at least one real
   external IdP (Okta dev org is free and the XAA counterparty —
   start there).
2. **7.8.2** Broker mode docs + probes (Decision 4). One session,
   mostly configuration and verification.
3. **7.8.3** EMA spike against xaa.dev, then the receiver
   (Decision 5). Spike first; the build is its own session and may
   wait on the ext-auth revision alignment.
4. Decision 3's reconciliation check rides ADR-007 D4's build
   (already backlogged P3), not a separate slot.

## Probe before building (the verify-the-vendor list)

- Okta: which discovery document our OIDC lib resolves for the org AS;
  whether XAA is exposed on a free dev org's plan.
- Entra: register a single-tenant app, confirm email-claim behavior
  and that `oid`/`tid` arrive in the id_token; confirm the door's
  exact-issuer validation passes with the tenant-specific issuer;
  recreate a test app registration and confirm whether `sub` changes
  (community-reported); re-verify Entra's RFC 8693 / EMA-roadmap
  status from a primary Microsoft source (the claim in Context rests
  on community reporting).
- Ping: PingOne trial — discovery from the per-environment issuer,
  which claims its id_token actually carries for our app vs userinfo,
  and (for Decision 5, later) which subject_token_types its RFC 8693
  accepts.
- Authentik broker: the six unknowns — group-sync lifecycle on the
  Entra source (adds documented, removals undocumented — irrelevant
  to Airlock under P1, relevant to portal apps), the
  source-reads-userinfo-not-id_token behavior (issue #21382), SCIM-
  source + OAuth-source account linking under identifier matching,
  whether the deployed 2026.x build carries SAML ForceAuthn, whether
  upstream sign-out really leaves the Authentik session alive
  (believed yes — confirm on the wire), and whether the deployed
  source-authentication flow is the stock no-MFA-stage blueprint.
- EMA: ext-auth repo revision vs 2026-07-28 core; Anthropic client
  IdP-genericity AND its beta/plan-gating status (recorded as
  waitlist-gated 2026-08-02); **client registration at our AS — the
  ext-auth flow must work with CIMD or static client_ids; if any
  implementation requires DCR, that is a blocker to raise with the
  owner, never a config to enable** (the DCR veto is permanent,
  ADR-005 D9a).

## Consequences

- The product claim becomes: *"Ships with its own IdP; connects to
  yours."* Authentik default → external OIDC IdP as first-class
  config → EMA for enterprises. Each rung is optional and additive.
- ADR-005 D2's named bound gets its stated fix on the Airlock path
  the moment a deployment points the door at an external tenant.
- ADR-005 D10's rung 1 (XAA delegated assertion to UPSTREAMS) loses
  ONE of its two blockers: an ID-JAG-issuing IdP now exists in the
  deployment. The other blocker — upstream resource servers (GitHub,
  Slack) accepting ID-JAGs — remains open, and Decision 5's receiver
  is the door's *login leg*, not layer 1: for rung 1 the *upstream*
  receives the assertion, not us. Layer-1 posture is unchanged from
  ADR-005 D9(b)/D10 — per-user OAuth stays the best available rung
  until upstreams accept the spec.
- The `authentik.lab.local` hardcodes across three consumer charts
  (openwebui, monitoring, argocd), Authentik's own chart, and the
  Sentinel installer/config defaults (install-systemd.sh,
  dev-stack.sh, config.py:161) become the
  visible remainder of the adopter-rule debt — this ADR does not
  sweep them (backlogged since 2026-07-25) but 7.8.1 must not add new
  ones.
- Two small pre-existing defects get fixed as drive-bys (the stale
  blueprint comment; the `/link` session-cookie path bug).

## Alternatives considered

- **Trust external-IdP group claims for authorization** (the "now the
  IdP is outside the agent's reach, why not?" argument). Rejected.
  P1's original premise does weaken — but console-owned entitlements
  survive on their own merits: the access matrix, elevation ladder,
  windows, and forbids are Sentinel's product surface and cannot be
  expressed as group claims; offboarding and audit must not depend on
  a third party's claim pipeline; and group-claim plumbing is exactly
  where the vendors are weakest (Okta caps the claim at ~100 groups;
  Entra silently degrades to a Graph pointer past 200). A future
  read-only *reconciliation* against IdP groups (flag drift, never
  auto-apply) is compatible with this ADR and belongs to ADR-007 D4's
  machinery.
- **Multi-IdP simultaneous.** Rejected for now (Decision 1). It is
  the account-linking minefield, and no near-term deployment needs
  two workforce IdPs at once. The (issuer, sub) composite pin keeps
  the schema ready if that day comes.
- **SAML at the door.** Rejected (Decision 6).
- **Replace the door's AS facade with an off-the-shelf product**
  (Cloudflare ships "Managed OAuth for Access" — vendor claim;
  ADR-007 also named WorkOS AuthKit and Descope from market memory,
  unverified this session). Rejected for the product: they are SaaS
  dependencies in
  the middle of the trust path, which breaks self-hosted compliance
  buyers (ADR-005 D9c) — and ADR-007 already noted the honest
  version: "the door might not need to be hand-built next time." For
  a from-scratch build the calculus differs; for us the facade exists,
  is small, and is where EMA lands.
- **Inbound SCIM now.** Deferred (Decision 3). Real enterprise
  checkbox, real work; TTLs + reconciliation + manual console
  offboarding are the honest interim, stated as such.
- **Do nothing (Authentik-only forever).** Rejected — it concedes the
  one table-stakes axis the field has converged on, leaves ADR-005
  D2's named bound unfixed on its stated path, and keeps D10 rung 1
  permanently unreachable.

## Non-goals

- Being an IdP for anyone else (no ID-JAG *issuing*, no user store
  ambitions beyond the shipped default).
- Autonomous-agent identity via XAA (spec-excluded; Mission Control
  stays PR-only; the classic flow-scoped capability path keeps its
  own already-backlogged design pass).
- Per-tenant SSO self-serve portals (multi-customer SaaS machinery —
  ADR-002's open "run vs connect" question decides if that ever
  matters).

## Sources (dated, as verified 2026-08-22)

Repo: `sentinel/app/{door.py,config.py,service.py,models.py,ladder.py,policy.py}`,
`sentinel/scripts/install-systemd.sh`, `catalog/authentik/templates/oidc-blueprints.yaml`,
ADR-004:229-302, ADR-005 (P1, D2, D5, D9, D10), ADR-007 D4.
External, confidence-tagged in the session record: MCP spec 2026-07-28
changelog + authorization section (RFC 9728/8707/8414, CIMD, DCR
deprecation); EMA extension page + MCP blog (stable 2026-06-18, Okta
first issuer, Anthropic/VS Code clients); Okta developer docs (org vs
custom AS, API Access Management, groups-claim cap, XAA); Microsoft
Learn (pairwise `sub`, `oid`+`tid`, groups overage 200/150/6, email
optional claim, nOAuth guidance); community reports, not primary
vendor docs (Entra `/common` issuer placeholder — spring-security
#17948 + oauth2-proxy docs; Entra no-RFC-8693 — MS Q&A + solo.io;
both on the probe list); Ping docs (PingFederate/PingOne RFC 8693,
per-environment discovery); Authentik 2026.8 docs (Sources, Entra/Okta
source types, SCIM source + its tenant-wide matching warning,
matching-mode warnings, default source-auth flow); Keycloak ID-JAG
nightly (receiver experimental, issuer TBD); WorkOS/EnterpriseReady/
Clerk enterprise-SSO checklists; Vaultwarden GHSA-6x5c-84vm-5j56;
NIST SP 800-63C; ToolHive/agentgateway/Obot/MintMCP/Cloudflare vendor
docs (marketing-sourced where not probed, marked accordingly).
