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
4. **7.4 — The GitHub MCP server.** Its own session; upstream and
   toolset decided in 7.1.

   **Clarified 2026-08-02 (owner confusion, and the owner was right):**
   *"wouldn't you be building the github mcp server in a portable
   scalable docker container in this infra we've created? it would
   almost COME with the stack?"* — **yes, exactly that**, and the
   earlier "give me your GHES hostname" framing was wrong. Nobody
   needs to own a GitHub Enterprise Server to do this phase. The MCP
   server is **a catalog chart like every other workload**: official
   `ghcr.io/github/github-mcp-server` image in native http mode, a
   Deployment behind the sentinel-proxy, `exposes-mcp: "true"`,
   fronted by the same enforcement path everything else uses. It ships
   WITH the stack; a new deployment gets it by dropping the chart in
   `catalog/`, exactly like OpenWebUI or Postgres.

   Only two things vary per deployment, and neither is
   infrastructure: **which GitHub it points at** (`github.com` by
   default; a company's self-hosted GHES is one values line —
   `GITHUB_HOST` — which is where the original "on-prem" idea
   actually lands, as a *variant*, not a requirement) and **the
   credential** (a fine-grained PAT in SOPS; App auth is stdio-only
   per 7.1, and there is no server-side repo allowlist, so the PAT's
   own scope IS the repo boundary — layer 1 of the three-layer rule).

   **Where scale comes from:** the server is a stateless HTTP proxy to
   an API, so it is `replicas` + the existing KEDA pattern like any
   other Deployment. The real ceiling is GitHub's per-token rate
   limit, not pods — which is a layer-1 credential question (more
   tokens / an App installation), not a Kubernetes one. Worth stating
   because "scale the MCP server" sounds like a cluster problem and
   is not.

   **How it completes the chain:** every "allowed by policy, but no
   upstream is configured" answer from 7.3 becomes a real API call —
   door → ladder → forwarding token → sentinel-proxy → this chart →
   GitHub. Nothing above it changes; that is the point of having
   built the gate first.

   **Why not reuse Mission Control's GitHub App (owner asked, and it
   is the right question).** Two independent reasons. *Technical:* the
   official server supports App server-to-server auth in **stdio mode
   only** (7.1 finding) — in http mode, which is what a
   catalog-deployed Deployment needs, the credential is a PAT.
   *Architectural, and the one that would still apply if the technical
   constraint changed:* the operator's App key is **Mission Control's identity**,
   held outside the cluster on the workstation. Putting it inside an
   in-cluster workload would (a) hand a compromised pod the identity
   that opens PRs on this repo, and (b) **collapse attribution** —
   every PR would read as "the operator", whether the platform
   proposed it or a person did through Airlock. Two flows that a human
   must be able to tell apart in the audit log need two identities.

   **"Scope the credential to everything and let Cedar limit repos and
   deletes" — the correction that makes it work.** Cedar only governs
   calls that come *through* the gate. A credential broad enough to do
   anything means anything that gets hold of it — a compromised pod, a
   bug in the proxy, a NetworkPolicy mistake — has full power on a
   path Cedar never sees. That is the single-layer failure the
   three-layer rule exists to prevent. The resolution is a division of
   labour, not a choice between them:

   > **The credential decides what is POSSIBLE. Cedar decides who does
   > it.** Scope the token to the blast radius you are willing to have
   > *at all*, then let policy govern everything inside it.

   Concretely for GitHub: grant Contents + Pull requests and **do not
   grant Administration** — then repository deletion is not forbidden
   by policy, it is *impossible*, and stays impossible even if
   Sentinel is wholly compromised. That is CLAUDE.md's `forbid` ("no
   button, ever") implemented in physics rather than in software, and
   it is strictly stronger. Meanwhile the token may cover **all** the
   org's repos — which is what makes this scale to 500 repos without
   minting 500 credentials — because *which* repo a given person may
   touch, at which tier, is exactly the question the matrix and
   `servers.yaml` resource map already answer.
5. **7.5 — The Slack MCP server.** Socket Mode (below). This is also
   where `#claude-audit` / `#claude-alerts` become real channels.
6. **7.6 — The record, made durable (added 2026-08-02, owner ask).**
   *"our LOGS will show that exact flow right? what they accessed WHO
   accessed it when… we'll want a database of sorts like 90 days and
   optional log streaming to a better service"* — split honestly into
   what already exists and what does not.

   **Already true today.** Every decision writes an `audit_events`
   row carrying `ts`, `principal` (the person's email), `tool`,
   `resource` (the derived object, e.g. `itdan-com/homelab`),
   `policy_version` (the exact policy that decided it), `flow_id`,
   `event_type` (use/denial/grant/…), and a JSON `details` blob with
   the outcome, reason, and grant id. So "who accessed what, when,
   under which authority, and was it allowed" is answerable **now**,
   per call, and the console reads it. This needs nothing from the
   GitHub MCP server: the record is written at the gate, which is why
   it works identically for every future server.

   **Not true yet — three gaps, in priority order.**
   1. **The effect is unrecorded.** We log the decision, not what the
      upstream did with it. A permitted call that GitHub then rejects,
      or that created PR #42, looks identical in the log. Fix: the
      door audits the upstream response — status, latency, error
      class, and the upstream's own object id where it returns one.
      **Deliberately not the payload**: PR bodies and file contents
      are a privacy and retention liability, and the upstream keeps
      its own content log. Small; belongs *with* 7.4.
   2. **No integrity.** `audit_events` is a mutable local table
      (ADR-004 debt 3): anyone with the DB file can edit history
      silently. Fix: a `prev_hash` chain so tampering is detectable,
      plus a verify command. ~20 lines and a migration.
   3. **No retention, rotation, or export.** The table grows forever
      and lives only on one host. Fix: a configurable window
      (default **90 days**), rotation that **seals and exports a
      segment rather than deleting rows** (deleting from a hash chain
      breaks it — segments get sealed with their terminal hash), and
      a JSONL sink. The sink is the streaming answer: Phase 8 already
      plans Loki and a "Claude actions dashboard", so Sentinel's
      audit becomes one more Loki stream, and any SIEM that reads
      JSONL or Loki works with no Sentinel change.

   Sequencing: (1) rides 7.4 because it is where the first real
   upstream appears; (2) and (3) are their own small session, before
   any second person uses Airlock in anger.

## 7.4 credential model — REVERSED by verification (2026-08-02)

The owner asked the right question — *"if I give you a PAT, does it
enable an entire workforce to use my token? that seems like an
oversight for self-hosted"* — and verifying it against the shipped
binary (not the docs) inverted the design:

**GitHub's MCP server has NO static token in http mode.** Its HTTP
`ServerConfig` has no token field; `GITHUB_PERSONAL_ACCESS_TOKEN` in
the environment is silently ignored; every request without an
`Authorization` header gets a 401. **Per-request credentials are the
only mechanism** (since v0.31.0). GitHub's own changelog claims a
static fallback — it is wrong, upstream issue #2946 reports the same,
and every secondary source repeats the error. The first chart was
built on that false premise and would have 401'd every call.

Consequences, all improvements:

- **The workload holds no credential at all.** Not a mounted secret —
  none. Compromising the MCP server pod steals nothing. Sentinel's
  door injects the credential on the call it just authorized.
- **This is GitHub's own architecture.** Their hosted server runs the
  same codebase behind an auth reverse proxy that supplies the
  token (maintainer, #471) — structurally identical to the Sentinel
  proxy. Independent validation of the shape.
- **App auth is stdio-only BY DESIGN, and the reasoning is ours:** a
  network-reachable server with a server-wide app identity would let
  any client that reaches the port act as the app (PR #2797). They
  refused the ambient-authority shape for the same reason we do.
- **A live security finding:** in http mode `X-MCP-Toolsets`,
  `X-MCP-Readonly` and `X-MCP-Exclude-Tools` are read **from the
  request**, so a caller could widen its own toolset. The door now
  overwrites all three on every forward, and the CLI flags stay the
  server-side ceiling — two places, because one is client-influenced.

**What remains true and must be said plainly:** with one shared token,
GitHub's audit log attributes every action to the token owner. There
is no "on behalf of" header — the header inventory is closed, and
maintainers say the auth layer *"should eventually convey 'on behalf
of' but we aren'''t quite there yet"* (#2201). So **Sentinel's audit log
is the only record of which human was behind a call**, which CLAUDE.md
already asserts and now has an external reason for.

**Named upgrade path, now known to be reachable:** the server accepts
`ghu_` user-to-server tokens over HTTP. So Sentinel brokering a
per-person GitHub token gives real per-user attribution at GitHub
without touching this chart — the door already injects; only the
source of the token changes. That is the concrete shape of ADR-005
D9'''s XAA line, and upstream closed #2224 saying they will support
XAA/ID-JAG when the spec does.

**Open decision for the build:** GitHub'''s maintainers note the shared
HTTP process was never hardened for many-user use (#471), and stdio'''s
supported multi-user shape is process-per-user (#132). "One Deployment
for everyone" vs "one pod per elevated session" is a real 7.4
decision, not a detail.

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
- [x] **7.3.2 The Authentik client for the door** (DONE 2026-08-02,
  LIVE — blueprint synced `08ebd5e`, discovery + jwks + allowlist
  discrimination verified on the wire). Static OAuth 2.1
  + PKCE client (auth-code flow) in Authentik via blueprint, and the
  CIMD exploration: observe what Claude Code ACTUALLY sends against
  an OAuth-guarded MCP endpoint (URL-shaped client_id? metadata
  document fetch?), verify whether Authentik 2026.5.6 can consume a
  URL client_id — statically registered with the CIMD URL as the
  client_id if native fetch is absent — and record the finding
  honestly (CIMD-shaped vs full CIMD). RFC 8414/OIDC discovery
  verified against the live Authentik; the redirect-URI story for a
  loopback CLI client settled here.
- [ ] **7.3.3 The door itself — re-cut 2026-08-02 (owner product
  frame): the door carries its own authorization-server facade.**
  Shipped Authentik is the end-state IdP, so the AS that MCP
  clients face is OURS — CIMD lands here, not with the
  external-IdP move (ADR-005 D9 amendment). `mcp.lab.local` with
  real TLS; topology decision recorded in-item (lean: a Sentinel
  HOST listener — the facade holds the key that MINTS trusted
  person-identity, and P1 puts that key outside the agent-writable
  cluster; in-cluster Envoy remains the enforcement point in front
  of MCP servers). RFC 9728 PRM at the door; AS metadata
  advertising CIMD; auth-code + PKCE (S256 only); client identity
  via CIMD document (SSRF-guarded fetch, redirect_uri validated
  with RFC 8252 loopback-port variance — neutralizes claude-code
  #37747 correctly) or static allowlist; **NO DCR, permanently**
  (owner). Human login federates to Authentik through the 7.3.2
  client (flips confidential; redirect becomes the door's own
  callback). The door issues its OWN short-lived RS256
  person-tokens, audience-bound to the door resource (honoring the
  client's observed RFC 8707 `resource` param) + rotating refresh;
  `/mcp` validates them and mints person-flow ids (gateway-minted,
  never client-chosen). Ladder wiring + cluster forwarding stay
  7.3.4 by design.
- [x] **7.3.4 Handshake birthright through the ladder** (DONE
  2026-08-02, code + tests; suite 84/84, 15 new). The door speaks
  MCP: `initialize` / `tools/list` / `tools/call` / `ping` /
  notifications / batches, one address fronting every server, tools
  namespaced `<server>.<leaf>` — the same string the ladder decides
  on and the audit log records. **Visibility became its own
  question:** `ladder.visible_tools()` evaluates policy per tool
  with NO grants, NO DB and deliberately NO audit rows (a listing
  would otherwise bury real denials under thousands), against the
  SERVER-level resource — "may you use this somewhere", with the
  concrete-resource question left to call time. A borrowable tool is
  listed and marked; a server whose handshake is forbidden is absent
  whole. Proven: engineering sees echo+github and hr-platform is
  invisible; hr-head sees the approve rung; **prod-tier write stays
  forbidden while the staging twin offers approval**; a refusal
  carries the elevation offer (profile + windows); holding the grant
  turns the same call into a pass; every decision audits with its
  `policy_version`. Stream caps: **no SSE, by construction** — the
  door is request/response only, so ADR-005 audit gap 1 (an
  elevation cannot close an open stream) has no surface here; when
  server-initiated notifications need SSE, the cap ships in the same
  change (`GET /mcp` → 405 saying so).
- [x] **7.3.5 The confirm + approve doors** (DONE 2026-08-02, code +
  tests; suite 96/96, 12 new). A refusal now carries a **one-time
  elevation link** alongside the offer, and the link is a **browser
  page behind the company IdP**, not an MCP tool — because an
  `airlock.elevate` tool is callable by the MODEL, and a model that
  can elevate itself is the self-granting hole this architecture
  exists to close (ADR-005: anything may draft, only a human
  activates). The MCP-native alternative is the spec's `elicitation`
  capability (Claude Code advertises it), which needs a
  server→client stream — named as the upgrade that ships WITH SSE,
  not pretended. `confirm` = the caller clicks a window from the
  matrix and self-issues (`granted_via=confirm`, `granted_by` =
  themselves); `approve` = the same click files a card on the
  passkey console, and `grant_request` now mints a PROFILE grant
  when the request carries a profile + principal
  (`granted_via=approve`, `granted_by` = the approver) — one
  primitive, two doors, exactly as ADR-005 predicted. Proven: link
  is single-use and person-bound (another signed-in person gets
  403); CSRF forgery mints nothing; only an offered window is
  accepted; **a self-issued grant never opens the approve rung**; an
  approved window still cannot write the forbidden prod tier; and
  the audit log reconstructs the window (grant row with via +
  minutes, every call under it stamped with `policy_version`).
  **THE MEASUREMENT, on the wire: handshake + list + three
  birthright calls = ZERO approvals; one write window = ONE
  deliberate human act covering every write in it** — against
  5.5.8's seven. Lab collapse stays named: one enrolled human means
  requester ≈ approver until a second passkey enrolls; the mechanism
  (console grant + window + `granted_by`) is enforced regardless.
- [~] **7.3.6 Install line + battery + demo + close** (builder half
  DONE; **LIVE 2026-08-02** — installed, owner signed in from real
  Claude Code, tools listed, a real call got a policy verdict.
  Remaining: the confirm-elevation click, then close). **Five bugs
  the live path found that nothing else could**, each fixed with the
  guard that would have caught it: (1) `python-multipart` unpinned —
  the dev venv had it, a clean install did not, and the door
  crash-looped; installer now import-checks the DEPLOYED venv before
  systemd runs; (2) the cluster-CA adoption ran `kubectl` as **root
  under sudo**, where HOME is reset and there is no kubeconfig, so
  the door fell back to the system trust store and failed every
  sign-in with `CERTIFICATE_VERIFY_FAILED` — now runs as `$SUDO_USER`,
  fails loudly for a private issuer, and a new wire probe reaches the
  IdP the same way the door does; (3) the door had **no index page**,
  so the first human to visit the address people are *given* got a
  bare 404 — it now explains itself; (4) `ca.crt` sat in the 0700
  key directory, so the first real client could not verify the door
  (`EACCES`) — the CA **certificate** is public by construction and
  is now published at `/etc/sentinel/ca.crt`; (5) **the sign-in
  redirect was the one IdP url a BROWSER follows and the only one
  missing the transport rewrite** — Authentik advertises the default
  port, the lab serves 8443, so the first sign-in died `connection
  refused` while every probe stayed green (regression test added).
  Pattern worth keeping: every one of these passed unit tests and the
  rehearsal; only a real human on a real client found them.
  Delivered: a **door leaf** in `mint-certs.sh` (SAN `mcp.lab.local`
  + loopback; cloud swaps in a publicly-trusted cert); the installer
  installs/enables/**restarts** the third unit, writes the door +
  OIDC env, and **adopts the cluster CA by detection** so the door
  verifies the IdP's TLS strictly; **wire probes extended** — door
  https 200, its advertised resource must equal the origin people
  type, an unauthenticated MCP call must 401 *with* the discovery
  pointer, and the two POLICY CONSUMERS (broker + door) must agree
  on the active version or the install fails; battery section 8
  drives the person path and prints BEFORE/AFTER; SETUP §1.6 rewritten
  for three units + new **§1.6b** (client setup, the
  zero-tools-until-you-are-in-the-store warning, `NODE_EXTRA_CA_CERTS`,
  why elevation is a link not a tool) and the backlog's certutil
  wording closed. **Authentik client re-homed** to the door
  (`08ebd5e`→`c3a15c8`): the provider now carries ONE strict redirect
  (the door's callback) instead of wildcarded ephemeral ports —
  verified live (door callback 302, old 18789 redirect 400).
  **Rehearsed against a live dev door before handing over the sudo
  line** — see notes: the real CIMD chain works end to end.
  Remaining (owner): `sudo ./scripts/install-systemd.sh`, add
  `bob@itdan.com` to the store via Access → People, then
  `docs/demos/airlock-the-door.md`.

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

## How a company actually makes Airlock the only path (owner question, 2026-08-02)

Asked at the first live sign-in: *"how is this locking down a
corporate MCP server list? the company would have to disallow local
MCP servers? … if I bought Claude Enterprise I could force all users
to only have access to airlock?"* The answer is layered, and the
strongest layer is not the one people reach for first.

**Airlock does not — and cannot — stop someone running their own MCP
server.** It governs what happens when a call arrives at it. Client
lockdown is an endpoint-management problem, not a gateway problem, and
treating the gateway as if it solved that is how architectures acquire
imaginary controls.

What actually makes Airlock the only path, in order of strength:

1. **Credential monopoly at the resource (the real control).** A
   personal MCP server pointed at the company's GitHub is harmless if
   it has nothing to authenticate with. That means: no personal PATs
   on company systems (SSO + SCIM, PAT creation disabled or
   org-restricted), machine credentials issued only to the Airlock
   deployment, and — where supported — IP allowlists naming the
   gateway. This is layer 1 of the three-layer rule already in
   CLAUDE.md, applied organisationally instead of per-server. It is
   the only layer that survives an unmanaged laptop.
2. **Network egress control.** If reaching `api.github.com` or a
   Snowflake endpoint from a corporate network requires the proxy, and
   the proxy only permits the gateway, an unsanctioned MCP server has
   no route. Defeated by a phone hotspot, so it is a speed bump on
   managed networks, not a boundary.
3. **Client-side managed policy — and for Claude Code this is real,
   not aspirational** (verified against current docs, 2026-08-02).
   A `managed-mcp.json` deployed at an OS-protected path takes
   **exclusive control**: users cannot add, modify or use any other
   MCP server, `claude mcp add` refuses outright, plugin-provided
   servers are blocked, and claude.ai connectors are suppressed
   unless explicitly re-enabled. Paths: `/etc/claude-code/` (Linux,
   WSL), `/Library/Application Support/ClaudeCode/` (macOS),
   `C:\Program Files\ClaudeCode\` (Windows). "Everyone gets Airlock
   and nothing else" is literally this file:

   ```json
   { "mcpServers": { "airlock": {
       "type": "http", "url": "https://mcp.<domain>/mcp" } } }
   ```

   Alongside it, `managed-settings.json` (same directories, or
   MDM/registry, or the claude.ai admin console for server-delivered
   settings) carries `allowedMcpServers` / `deniedMcpServers` /
   `allowManagedMcpServersOnly`. Two properties worth knowing:
   `--mcp-config` and `--strict-mcp-config` do **not** bypass the
   allow/deny lists, and `deniedMcpServers` merges from every source
   so a user cannot clear it. One trap: matching by **`serverName` is
   explicitly not a security boundary** — a user can label any server
   `github` — so enforcement rules must match on `serverUrl` or
   `serverCommand`. Note the split: the admin console can deliver the
   allow/deny settings, but `managed-mcp.json` itself needs MDM or
   OS-level deployment. And it all still governs the CLIENT, so it is
   exactly as strong as device management — which is why it ranks
   below credential monopoly, not above it.
4. **Making the sanctioned path the easy one.** Birthright
   entitlements at zero approvals and `confirm` elevation that beats
   an IT ticket. Shadow IT is usually a friction symptom: people route
   around gates that cost more than the work. This is the layer this
   whole phase exists to build, and it is why the seven-approvals
   finding mattered.

**The honest limit, stated so nobody oversells it:** an engineer with
their own laptop and their own credential to a system can always
bypass any gateway. What Airlock buys is that every *sanctioned*
action is entitled, time-boxed, and recorded — and that the
unsanctioned path requires deliberately obtaining a credential the
company can refuse to issue. That is an administrative and credential
boundary with technical enforcement, not technical omnipotence.

**Product consequence for the open-source story:** "deploy Airlock"
is not a complete answer to MCP governance, and the README should not
imply it is. The complete answer is "deploy Airlock *and* stop issuing
direct credentials to the systems behind it" — which is a policy
change most compliance-driven buyers are already trying to make and
lack a usable alternative for. That framing is the product.

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
- **2026-08-02 (7.3.2) — the CIMD exploration, observed not assumed
  (probe rig in scratchpad, three AS modes, Claude Code 2.1.220,
  protocol 2025-11-25 on the wire):** the client's registration
  preference chain (MCP spec 2026-07-28) is CIMD → DCR →
  pre-configured → prompt, and the CIMD branch is **gated on the AS
  advertising `client_id_metadata_document_supported: true`** — with
  the flag up, the authorize URL carries
  `client_id=https://claude.ai/oauth/claude-code-client-metadata`
  (captured via `claude mcp login --no-browser`), S256 PKCE, and an
  **RFC 8707 `resource=` parameter** naming the MCP endpoint (7.3.3
  audience note: Authentik won't consume it); with DCR advertised it
  POSTs RFC 7591 registration (observed); with neither + no static
  id, headless defers to a user prompt. **Decision: static
  registration, not CIMD-shaped strings** — Authentik 2026.5.6 will
  never advertise the flag, so a URL-shaped static client_id would
  never even be presented by the client; and upstream issue #37747
  (v2.1.80+ publishes PORTLESS redirect_uris in the CIMD document)
  would break strict-validating CIMD servers anyway. Redirect story:
  the callback port is RANDOM per login (observed 3118 → 54491)
  unless pinned client-side, so the client config pins
  `oauth.callbackPort: 18789` and the provider allowlist stays
  `matching_mode: strict` (both loopback spellings; regex allowlists
  rejected as a foot-gun). Blueprint shape decisions: PUBLIC client
  (no secret a workstation must keep; PKCE is the defense),
  code+refresh grants only per-provider (the B5 empty-allowlist
  lesson doubles as OAuth 2.1 enforcement — the server-wide metadata
  still ADVERTISES implicit/password globally, recorded honestly;
  the provider refuses them), **no group policy binding and no
  roles/groups claim — ADR-005 P1 at the token layer**: Authentik
  answers WHO, the policy store alone answers WHAT (store-unknown
  person = valid token, forbid on every call); flagged for owner
  review, reversible with one policybinding entry. Verified live:
  discovery at `/application/o/mcp/`, jwks = 1 RS256 key (the
  broker's 7.3.3 validation anchor), pinned redirect → 302 into
  login flow, wrong redirect → 400 no-redirect. Open for 7.3.3:
  refresh-token UX (`offline_access` scope not attached yet — match
  to observed client behavior when the first real token mints);
  demo sign-in path is `claude mcp login <name> --no-browser`
  (v2.1.186+). Full token mint deliberately NOT yet exercised — it
  needs 7.3.3's door.
- **2026-08-02 (7.3.3):** a bug the tests caught that production
  would have shown as a mystery — `models.utcnow()` is NAIVE UTC (the
  DB convention) and `.timestamp()` on a naive datetime applies the
  HOST's local offset, so every door token was stamped six hours into
  the future on this MDT box and any correct validator would call it
  not-yet-valid. JWT claims now come from `time.time()`. Transferable:
  two time conventions in one process, one of them invisible at the
  call site. Also recorded: the door's upstream leg to Authentik is
  still a PUBLIC client using PKCE rather than confidential with a
  secret — spec-legal, works, and named as a hardening item rather
  than quietly left (SOPS + blueprint flip when touched).
- **2026-08-02 (7.3.4) — the finding that will bite the demo if
  forgotten:** a person the POLICY STORE has never heard of sees
  **zero** tools, not even the `all-employees` birthright, because
  that group's membership is a fact of the entity store, not of the
  IdP (7.2.3's "store-unknown person = forbid", now proven at the
  visibility layer too). So onboarding is a policy-store edit in the
  console's Access screen, and an SSO login alone buys nothing —
  **the owner's own email must be added to the store before the 7.3.6
  demo can list a single tool.** Second bug caught by tests, not by
  production: the door passed `{"arguments": {...}}` into
  `ladder.decide()` where the store's resource map walks the
  `params.arguments` RECORD itself, so every resource-mapped tool
  derived `unmapped-resource` and denied closed — safe, and silently
  unusable. Also: `SENTINEL_MCP_UPSTREAMS` keeps each server's URL in
  CONFIG, never in the policy store, so a console policy edit can
  never retarget traffic to another host; a server with no upstream
  is decidable but not callable, which is the honest state until 7.4.
- **2026-08-02 (7.3.6) — the CIMD chain PROVEN on the real wire
  before the owner touched anything.** A dev door was run with the
  exact env the installer writes, against live Authentik and the
  real internet: `/authorize` with Claude Code's genuine client_id
  (`https://claude.ai/oauth/claude-code-client-metadata`) → the door
  fetched that document through the SSRF guard → **matched its
  PORTLESS `http://localhost/callback` against a real ephemeral port
  via RFC 8252 §7.3** (claude-code #37747 neutralized by
  spec-correctness, as designed) → 302 to Authentik carrying the
  door's own PKCE. Then the actual client: `claude mcp login` chose
  the CIMD branch (our metadata flag), sent `scope=mcp` (from our
  `scopes_supported`), and accepted our TLS via
  `NODE_EXTRA_CA_CERTS` — stopping only at the interactive
  paste-back, which is the owner's part. **The rehearsal caught a
  real failure first:** with `SENTINEL_OIDC_HTTP_BASE` unset the door
  fetched the issuer at :443 and died `ConnectError` — the installer
  sets it, but the lesson is that the issuer/transport split has a
  hard dependency, so the env write is load-bearing, not cosmetic.
  Also confirmed: `authentik.lab.local` resolves from WSL through
  Windows DNS, so the door uses the REAL hostname and verifies TLS
  strictly (no Host-header/SNI mismatch fudge).
- **2026-08-02 (7.3.6, forwarding) — the door goes THROUGH the
  enforcement point, not around it.** An allowed person-call now
  reaches a real MCP server the same way in-cluster callers do:
  `service.mint_forwarding_token()` issues a one-call, 30-second,
  scope-locked token AFTER `ladder.decide()` allowed, and the proxy
  independently re-checks the kill switch and derives scope from the
  request itself. Costs zero human taps (the approval question was
  answered upstairs); a leaked copy buys one call it was already
  entitled to make. Rejected alternative: letting the door talk
  straight to MCP services — that would make "nothing reaches an MCP
  server without a capability check" read "…unless it came from the
  door". **Three infrastructure findings:** (1) the host-side path to
  the proxy was a **root `kubectl port-forward` running since
  2026-07-28** — undeclared, reboot-fatal, invisible to
  `bootstrap.sh`; replaced with a chart IngressRoute; (2) k3s Traefik
  refuses **ExternalName** services AND cross-namespace service refs
  by default, so the route sits in the controller namespace targeting
  the data-plane service by the chart's deterministic name helper
  rather than loosening two global flags; (3) upstream MCP servers
  may answer a POST as JSON *or* as a one-message SSE stream —
  the door handles both, because which one is the server's choice.
  Live-only bug: the ORM principal crossed a closed session boundary
  and raised `DetachedInstanceError` on the first real call; identity
  now travels as plain values. **Proven live: door → ladder → mint →
  proxy → broker check.** The last hop returned `unknown-token`
  because a DEV door writes its own SQLite while the installed broker
  reads `/var/lib/sentinel/sentinel.db` — which is the two processes
  disagreeing about the database, not the design; both units share
  that file after the install, and the proxy refusing a token it
  cannot verify is the gate working.
- **2026-08-02 (owner, mid-7.3 — the product frame that re-cut
  7.3.3):** *"we would be using authentik in an end state would we
  not? easy enough for a small company"*; *"im not sure why we
  can't use cimd but we are NOT using dcr its not secure"*; the
  stack terraform-deploys WITH self-hosted MCP servers alongside
  (compliance buyers self-host — their own GitHub MCP for GHES,
  custom servers) and must grow the common enterprise set
  (Snowflake named first). Consequences applied same hour: ADR-005
  D9 amended (CIMD via the door's own AS facade NOW; XAA posture
  unchanged); 7.3.3 re-cut to carry the facade; DCR recorded as a
  standing owner security stance, not an accident of Authentik's
  feature set; common-servers catalog added to STATUS backlog;
  7.3.2's `mcp-door` client re-homes as the facade's confidential
  upstream leg (blueprint flip rides 7.3.3's build).
