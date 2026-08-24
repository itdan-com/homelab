# ADR-011: Sentinel horizontal scaling — stateless brokers, shared Postgres, one sealer, one kill switch

**Status:** **Accepted** (2026-08-23, owner — decision #14, *"Accept
and I'll push"*). Proposed and accepted the same session,
written after the owner's challenge — *"i dont like that sentinel
can't horizontal scale"* — from a four-agent recon that verified the
hot path, the audit hash chain's concurrency model, the SQLite→
Postgres delta in this codebase, and ADR-004's trust invariants. The
short version the recon produced: **Sentinel's throughput was never
the wall, the gap is single-instance HA, and the horizontal path is
half-built already** — but it has real hazards that make it worth a
designed decision instead of a cost-doc footnote.

A three-lens adversarial review (code-accuracy, trust-invariants,
Postgres-correctness) then hardened this draft and is reflected below.
It confirmed the direction and the code claims, but **refuted the first
cut of the sealer fix** (a wall-clock settle window — unsound, because
`ts` is a per-instance client clock, not commit order; Decision 3 now
uses a transaction-visibility horizon) and added three trust invariants
the first draft left implicit: a **fail-loud topology interlock** so a
dropped config value can never silently fork the kill switch across
brokers (Decision 1), **per-role least privilege on the shared DB** so a
compromised broker cannot reach the kill switch at the storage layer
(Decision 5), and **placing the DB's Terraform on the agent-unreachable
side** of ADR-010's split (Decision 5). None required a redesign; all
are folded in.

## Context — what "can't scale" actually meant, corrected

`cloud-cost-shape.md` called Sentinel "the honest bottleneck (a VM,
not a Deployment — cannot autoscale)." Grounding that against the code
sharpens it into three separate claims, only one of which is a real
gap:

- **Throughput is not the wall.** The hot path — `check_capability`
  behind Envoy `ext_authz` — is **two indexed reads** on the agent hot
  path (the kill-switch singleton + a `token_hash` lookup) and **one
  small append** (the audit row), on SQLite in WAL mode. (The
  person-forwarded path adds a third indexed read — a PK lazy-load of
  `grant.principal` for the audit email — still O(1), immaterial to
  throughput.) WAL does thousands of these per second on one core;
  enterprise concurrency at human-paced tool calls is *hundreds* per
  second. SQLite is not the ceiling people assume for this read-heavy,
  tiny-write, low-QPS workload.
- **The real gap is HA.** Sentinel is a **single VM**, so it is a
  single point of failure: if it dies, `ext_authz` fails closed and
  every MCP call fails. That is an availability problem, not a
  throughput one — and it is the legitimate half of the owner's
  concern.
- **The design is half-built for the fix.** The broker is *already* a
  separate process from the admin console and kill switch (three
  uvicorn units); the audit layer *already* assumes concurrent
  unsealed writers from multiple processes with a single sealer
  (`audit_chain.py`'s own docstring); and the broker holds **no
  authoritative in-memory state** — every request/grant/token/nonce
  fact lives in the DB (the recon enumerated every dict; the
  authoritative ones live in the **door/EMA layer, not the broker** —
  `door.py`'s `_codes`/`_pending`/session-signing key/`_tickets` and
  `ema.py`'s `_seen_jti` replay cache; the broker's only module-level
  objects are a regex and the FastAPI app, and it re-reads kill state
  from the DB on every check with no cache). So "make the broker
  N-instance" is mostly *swap the shared storage*, not a rewrite.

**ADR-004 never forbade this.** Its "one droplet per platform is the
honest unit" line is a *complexity* claim about running two writers in
two different trust domains (cloud + local), explicitly "the
complexity SQLite was chosen to avoid" — not a throughput bar and not
a rule against N stateless brokers behind one shared DB. ADR-010 names
this exact upgrade ("shared-Postgres-instead-of-SQLite + instances")
as expected future work.

## Decision 1 — the topology: N stateless brokers + shared Postgres, one isolated admin/kill/sealer

Split what already wants splitting:

- **Broker (hot path) → stateless, horizontally scaled.** 2-N broker
  instances in an autoscaling group behind an **internal L4
  pass-through load balancer** (the cluster's Envoy `ext_authz` calls
  the LB, not a single box), every instance identical and disposable,
  all reading/writing the **shared Postgres**. This gives both HA (an
  instance dies, the LB routes around it) and throughput headroom (add
  instances). Each instance independently rebuilds its disk-derived
  policy cache from a shared/replicated policy store, converging within
  `POLICY_RELOAD_SECONDS` exactly as broker-vs-admin already do.
  - **The LB must be L4 pass-through (NLB), never a TLS-terminating
    L7 balancer (ALB).** The broker's *only* caller-identity control is
    mutual TLS enforced by uvicorn `--ssl-cert-reqs 2` — the Envoy
    fleet holds the sole client identity, checked at the TLS layer, with
    no app-layer client-cert check behind it. A TLS-terminating LB
    strips that mTLS gate, and then any host inside the trust VPC that
    reaches the LB could call the capability routes unauthenticated. So
    end-to-end mTLS from Envoy to the broker is an invariant: the LB
    forwards TCP, it does not terminate TLS.
- **Admin console + kill switch + sealer → ONE isolated instance,
  never scaled.** This is deliberate and load-bearing: one human
  operates the console, and *multiplying kill switches is a security
  anti-pattern*. It stays loopback-bound + SSM-tunnelled (ADR-004/
  ADR-010), stays outside the broker autoscaling group, and remains
  the **sole sealer** of the audit hash chain (Decision 3). Nothing
  here changes from today except that it now writes to Postgres.
- **ADR-004's trust invariants are preserved *on the normal path*,
  and Decision 5 closes the one new hole.** Verified by the review:
  still one trust *domain* outside the cluster; the cluster still has
  no path to the admin API (the brokers expose only the mTLS check
  port; the admin listener is loopback on a *different* instance the
  broker ASG does not include); the kill switch is still outside the
  cluster and still fails closed; and no broker exposes a kill or
  grant HTTP route. **What *is* new:** promoting the store to a shared
  network Postgres hands every broker a DB credential, and a broker
  compromised through its cluster-facing capability endpoint could,
  at the *storage* layer, `UPDATE kill_state`, insert a grant, or
  delete unsealed audit — bypassing the admin-only application gate.
  This is not preserved by topology alone; it is closed by **per-role
  least privilege on the DB (Decision 5)**, which SQLite could not
  express and Postgres can. The claim is therefore "preserved *with*
  Decision 5," not "preserved for free."

**This is opt-in, not a cutover — SQLite single-node stays the
default.** "Domain in, platform out" for a small deployment must stay
a zero-dependency single VM. So Sentinel gains **dual storage
support** (Decision 2): SQLite on one VM for the lab and small tiers;
Postgres + N brokers as the enterprise-tier opt-in, selected by a
config value, not a fork. The sizing profiles (phase-09) decide which
tier gets which.

- **The opt-in has a fail-loud interlock — a dropped config value must
  refuse to serve, never silently fork the kill switch.** This is the
  single most dangerous failure mode of dual-backend, and it is
  load-bearing enough to be an invariant. Today the SQLite default path
  is a *per-instance writable file*; if the clustered deployment simply
  "selects Postgres by env var," then a missing or mistemplated
  `SENTINEL_DATABASE_URL` on a broker does not fail — it silently falls
  back to a local SQLite file. Each such broker then reads its **own**
  `kill_state` row (default `engaged=false`) and its own grants: the
  operator engages the kill switch on the admin instance, and brokers on
  local SQLite never see it — **N diverging kill switches**, the one
  control that must never fail open, disabled by a typo. Guard: an
  explicit `SENTINEL_TOPOLOGY=clustered` mode that (a) **refuses to boot
  on a `sqlite://` URL**, (b) makes a missing `SENTINEL_DATABASE_URL` a
  **hard startup error** with no writable local-file default, and (c)
  verifies at startup that broker and admin resolve to the **same shared
  store** (a DB-identity/heartbeat check). A dropped value degrades to
  *refuse to serve* (`ext_authz` fails closed — safe), never to
  *per-instance kill switch* (fails open — catastrophic).

## Decision 2 — SQLite → Postgres as a supported second backend (the real work)

The recon graded this **moderate-to-hard**, and named exactly where:

- **`db.py` / `config.py` wiring — moderate, mechanical.** Today
  `DB_URL` is an f-string hardcoding `sqlite+pysqlite:///`, and the
  engine unconditionally passes `check_same_thread=False` plus a
  `connect` listener firing SQLite `PRAGMA journal_mode=WAL` etc.
  (psycopg rejects both). Fix: accept a full `SENTINEL_DATABASE_URL`,
  and **gate every SQLite-ism on the dialect** — PRAGMAs and
  `check_same_thread` only when `url.startswith("sqlite")`. Add the
  `psycopg` driver to requirements.
- **The models are nearly clean.** The naive-UTC convention maps to
  Postgres `timestamp without time zone` unchanged; enums are already
  `native_enum=False` + CHECK (portable on a *fresh* schema). Two
  additions the correctness decisions need: an `insert_txid` and a
  `seal_seq` column on `audit_events` (Decision 3). One cheap
  improvement: the three `JSON` columns should map to `JSONB` on
  Postgres for indexability.
- **The migrations are the blocker, and they get a clean answer.** The
  Alembic history is authored around SQLite `batch_alter_table` /
  `copy_from` / `recreate` idioms, and four enum-widening migrations
  would *silently fail to update the CHECK constraint* on Postgres.
  Replaying that SQLite-shaped history against Postgres is the trap.
  The decision: **Postgres gets a single squashed baseline migration**
  that stamps the *current* schema directly (the models are the source
  of truth), rather than replaying ten SQLite-batch steps — with the
  SQLite history left intact for existing single-node installs. A
  fresh Postgres deployment starts at that baseline; there is no
  SQLite→Postgres *data* migration in scope (a new enterprise
  deployment starts empty, and the audit segments export/import
  covers the record if ever needed).
- **Tests gain a Postgres path — including a chain-under-concurrency
  probe.** The suite sets `SENTINEL_DB` to a SQLite tempfile per module;
  the dual-backend work adds an opt-in `SENTINEL_DATABASE_URL` CI path
  against a throwaway Postgres (container), so the second backend is
  proven, not assumed. Crucially it adds the test that actually exercises
  Decision 3: **two sessions insert audit rows and commit in *reverse*
  id order (the lower id commits second) with a seal pass interleaved
  between the commits, then assert `verify()` returns ok.** That test
  *fails* against the wall-clock settle window and *passes* against the
  `xmin`-horizon + `seal_seq` fix — probing the correctness claim the way
  the repo's "probe the live path" discipline demands, rather than
  trusting the argument.

## Decision 3 — the sealer under Postgres: transaction-scoped advisory lock + a commit-visibility horizon + a gapless seal-sequence

The single-sealer model already exists; Postgres adds three
requirements. The review refuted the first cut of this decision (a
wall-clock settle window) as *unsound*, so the mechanism below is the
corrected one.

- **Serialize the sealer with a *transaction-scoped* advisory lock.**
  Today "only the admin process seals" is enforced by there being one
  admin process. On Postgres, wrap the seal pass in
  `pg_try_advisory_xact_lock(<sealer-key>)` so that even a
  misconfiguration running two sealers cannot fork the chain — the
  second simply skips. It must be the **`_xact_` (transaction-scoped)**
  variant, not session-scoped: `seal()` already commits per pass, so the
  lock auto-releases on commit/rollback, and it stays correct under the
  connection pooling (RDS Proxy / pgbouncer transaction mode) the
  enterprise tier uses — a session-scoped lock left on a pooled
  connection would be seen as held forever by a later pass on a
  different connection and silently halt the sealer. On SQLite the lock
  is a no-op (single writer already).

- **Seal by *commit visibility*, not by wall clock — the ordering
  hazard, corrected.** `seal()` walks unsealed rows `ORDER BY id ASC`.
  SQLite serializes writers so ids are monotonic in commit order;
  **Postgres sequences are not** — a transaction can grab id 100 and
  commit *after* id 101 already committed and got sealed, leaving id 100
  unsealed *behind* the chain head. The first draft tried to fence this
  with `ts < now() - 5s`; that is unsound, because `ts` is a Python-side
  `default=utcnow` — the *writing broker's own wall clock at flush*, not
  a commit timestamp and not commit order. Across N broker clocks plus a
  separate sealer clock, and with commit-vs-flush lag (a lock wait — e.g.
  Decision 4's `SELECT ... FOR UPDATE` — or a GC pause can hold a low-id
  row uncommitted well past 5s), a clock skew or a slow commit lets an
  old-id row land after later rows were sealed, **forking the hash
  chain** and raising a false tamper alarm on `verify()`. Wall-clock
  time cannot bound commit order.

  The correct fence is a **transaction-visibility horizon**. Add a
  server-side `insert_txid bigint` defaulted to `pg_current_xact_id()`,
  and seal only rows whose `insert_txid` is below the snapshot xmin —
  `insert_txid < pg_snapshot_xmin(pg_current_snapshot())`. Every such
  transaction has *already committed or aborted*; none can still commit
  an earlier row. This is immune to clock skew and commit lag by
  construction. (`ts` may stay as a cheap coarse pre-filter, never as
  the correctness boundary.)

- **Chain by a gapless `seal_seq`, not by the leaky sequence id.**
  Because id-order ≠ commit-order (and Postgres sequences leave
  permanent gaps from rolled-back transactions, so an id-contiguous
  prefix is unachievable anyway), the chain must not be defined by `id`.
  The single sealer — already holding the advisory lock and one snapshot
  — assigns a **gapless `seal_seq`** in commit-visible order as it seals,
  and `seal()`/`verify()` chain and walk by `seal_seq`. This makes the
  chain reproducible for a verifier regardless of insert-id order, and
  bumps `canonical()`'s version (`audit_chain.py`) as the schema change
  requires. On SQLite, `seal_seq` tracks id-order exactly (single
  writer), so existing single-node installs are unaffected.

  (Integrity is *preserved*, not merely "unchanged": what a row's
  predecessor *is* now derives from committed order, which is the
  property a tamper-evident log actually needs.)

## Decision 4 — kill-switch correctness under shared state

The recon flagged that two current correctness arguments are
SQLite-writer-serialization-specific and must be restated for Postgres;
the review sharpened both against RDS failover behavior:

- **Kill state is read from the WRITER ENDPOINT, fail-closed, never a
  replica, never cached.** "Kill wins every race" depends on every
  check re-reading the live row. "Pin to the primary" is under-specified
  and incoherent under failover — the primary *moves*. The invariant is:
  read via the cluster **writer endpoint** with
  `target_session_attrs=read-write`, and before trusting any "kill OFF"
  result assert `pg_is_in_recovery() = false`. Then treat **any**
  kill-read failure — timeout, read-only result, replica, the
  30–120 s RDS failover window when no writable primary exists — as
  **kill ENGAGED (deny)**, never as kill-off. That turns the failover
  window into a bounded *fail-closed deny* window, not a stale-authorize
  window (during which a killed platform would keep authorizing — the
  exact failure the invariant exists to stop). No broker-side cache of
  `KillState`.
- **`grant_request` and the kill writes share one explicit lock — not
  a dirty-check accident.** Today `grant_request` re-reads the switch
  and relies on "SQLite serializes writers." On Postgres MVCC that is
  not automatic. The subtle trap the review found: making only
  `grant_request` take `SELECT ... FOR UPDATE` on the kill row does
  **not** serialize it against a kill *engage*, because a repeat kill
  press never dirties the row (`engage_kill` reads via a plain `get` and
  on `first_press == False` emits no `UPDATE`), so no lock conflict
  forms. Fix: **both** `grant_request` and `engage_kill`/`release_kill`
  take the *same* lock on the kill row (both `SELECT ... FOR UPDATE`, or
  both the advisory lock), so mutual exclusion rests on an explicit
  protocol, not on SQLAlchemy's dirty-checking side effect. This is the
  low-QPS grant/admin path, so the lock cost is irrelevant.
- **The sealer gets its own connection and a chunked batch.** Decision 1
  co-locates sealer + kill + console on one admin instance, and a seal
  pass hashes+`UPDATE`s up to 5000 rows before a single commit. On a
  shared connection that large transaction can sit in front of a
  concurrent kill *write*, and kill latency is a security property. Give
  the sealer a dedicated (non-pooled) connection and bound its batch into
  smaller chunks so a seal never delays a kill.

## Decision 5 — the shared Postgres is canonical trust-domain state

Making the DB shared promotes it from "a file on Sentinel's host" to
**canonical trust-domain state** — it holds the kill switch, the
capability grants, and the canonical audit log. ADR-004's secrets/
mTLS sections cover only the three host secrets, not a shared DB, so
this ADR extends them:

- **The Postgres lives in the Sentinel trust domain, not the
  cluster** — a managed Postgres (RDS) or a DB on a trust-domain VM,
  in the trust VPC/subnet, reachable **only** by the broker instances
  and the admin instance via security-group source references; the
  cluster has no path to it, same one-way rule as the broker's own
  admin API.
- **Per-role least privilege — the broker credential cannot reach the
  kill switch or mint a grant.** A shared network DB means every broker
  holds a DB credential, and the broker is the one tier the cluster can
  reach (its capability endpoint); a broker RCE must not become a
  storage-layer bypass of the admin-only gate. Postgres makes this
  expressible where SQLite could not, so it is an invariant, not a
  nicety. Three roles:
  - **broker role:** `SELECT` on `kill_state` and `capability_grants`;
    `INSERT` on `audit_events` (no `UPDATE`/`DELETE` — which also makes
    pre-seal audit rows tamper-resistant); `INSERT`/`UPDATE` on
    `capability_requests` and `flows`. **No write to `kill_state`, no
    `INSERT` on `capability_grants`.**
  - **admin role** (the isolated console/kill/sealer instance): the
    kill writes, and the sealer's `UPDATE` of `audit_events`
    (`seal_seq`/`row_hash`/`prev_hash`).
  - **door role** (single-instance door): `INSERT` of
    `capability_grants` and forwarding tokens; no kill write. The door
    is a third writer of authoritative rows to this shared DB — named
    here so its placement and privilege are explicit even though
    scaling it is a non-goal.
- **Encrypted at rest and in transit** — KMS-encrypted storage, TLS
  (or mTLS) on the connection, the credential fetched by the
  instance's role (never in Terraform state or an env var — ADR-004's
  invariant, extended to the DB credential).
- **The DB's Terraform lives on the agent-unreachable side of ADR-010
  Decision 4.** ADR-010's split (trust-boundary Terraform lives where
  the operator's repo-scoped GitHub token cannot PR it) was written when
  Sentinel state was SQLite-on-VM; its enumerated agent-unreachable list
  does not mention a shared DB. This ADR **extends that list** to
  include: the RDS/DB instance, its **security-group source
  references**, the **DB role/credential definitions** (broker vs admin
  vs door), and the **internal-LB security group**. These are applied by
  the owner, never by a Mission Control PR — otherwise the operator agent
  could open an innocuous-looking PR adding the cluster's SG as a source
  on the DB (a direct cluster path to the kill-switch store) or
  broadening the broker's DB role, merged among a dozen config changes:
  ADR-004 reason 1, reconstituted. Only genuinely-ordinary substrate
  (e.g. the ASG launch template's non-trust bits) stays in-repo.
- **Availability is now a dependency, and kill durability needs a
  synchronous standby.** "Kill works even if the cluster is broken"
  still holds (kill is engaged on the isolated admin instance, enforced
  fail-closed at the broker per Decision 4), but it now also depends on
  the DB being up — and a DB failover is a platform-wide external-action
  *outage* (brokers fail closed, which is the safe direction, but every
  MCP call stops until the writer returns). Two consequences to name
  rather than discover: the failover target must be a **synchronous
  Multi-AZ standby, not an async read replica** (an async replica can
  lose a just-committed kill on failover — a kill that looks released),
  and both the kill read and the kill write go to the single writer
  endpoint. The small tier keeps SQLite and none of this exists.

## Scope, sequencing, and what this is NOT

- **Not required to launch Phase 9.** Single-node SQLite Sentinel is
  correct for the lab and the small/mid sizing tiers. This ADR is the
  **enterprise-tier deliverable**, sequenced with the sizing-profile
  work (phase-09) where "prove the 1000-person tier" is what actually
  demands it. It can be built and lab-tested (against a container
  Postgres) with no cloud account.
- **The build is bounded:** dual-backend `db.py` + the topology
  interlock (Decision 1) + the Postgres baseline migration (with the
  `insert_txid`/`seal_seq` columns) + the sealer's commit-visibility
  horizon and transaction-scoped advisory lock + the shared kill/grant
  lock + the per-role DB grants + a Postgres CI path. Plus the Terraform
  (an internal **L4** NLB, the broker ASG, the RDS in the trust subnet,
  its SG and DB roles on the **agent-unreachable** side) which lands with
  the Phase 9 `infra/` work.
- **"No broker logic changes" is *mostly* true, with two named
  exceptions.** The broker holds no authoritative in-memory state, so
  the storage swap needs no business-logic rewrite — but two paths rely
  on SQLite's single-writer serialization and need MVCC-safe handling:
  - **Idempotent get-or-create / dedupe.** `create_request`'s Flow
    get-or-create (`if s.get(Flow, id) is None: s.add(...)`), its
    pending-request dedupe (a select-then-insert with no backing unique
    constraint), and the `KillState`/`IdpMigration` singleton bootstrap
    can double-insert or PK-collide when two brokers first-touch the same
    row concurrently. These get `ON CONFLICT DO NOTHING` / `IntegrityError`
    handling (and the dedupe optionally a real unique constraint) — small,
    local, not a redesign.
  - **The ADR-007 velocity rail becomes best-effort under scale-out.**
    `actions_in_window` counts `USE` audit rows fresh on every Cedar
    eval; under N brokers on read-committed Postgres, a burst spread
    across brokers lets each `COUNT` see only its own snapshot, so the
    aggregate can exceed the threshold while each broker reads under it.
    Accept it explicitly as a *rate rail* (best-effort, eventually
    consistent), not a hard gate — or, if it must be exact, count it under
    the same writer-endpoint/serializable read as the kill path. Either
    way, the unqualified "holds no authoritative state" does not extend to
    this derived signal, and the ADR says so.
- **NON-GOAL: scaling the DOOR.** The door holds genuinely
  authoritative in-memory state — `_codes`/`_pending` (OAuth login
  state), `_seen_jti` (EMA replay cache), the session-signing key,
  `_tickets`. N door instances would need a shared store or sticky
  sessions for those; that is a *separate* problem, named here so it
  is not assumed solved. The door is also lower-QPS (interactive
  sign-ins, not per-tool-call), so it is a later concern.
- **NON-GOAL: scaling the admin/kill/sealer.** It must stay singular,
  by design.

## Consequences

- The owner's concern is answered concretely: Sentinel *can* scale
  horizontally, the path preserves every trust property (with Decision
  5's per-role scoping closing the one new storage-layer hole), and it
  is a bounded storage-swap because the architecture was already
  concurrent-writer-shaped. "Can't scale" was single-instance-HA, and
  it is now a specified, opt-in fix.
- Sentinel gains a second, production-grade storage backend without
  losing the zero-dependency SQLite default — the small deployment
  stays simple, the enterprise deployment scales.
- The correctness and trust properties that single-writer SQLite made
  implicit are now written, testable, Terraform-enforceable invariants:
  the sealer's commit-visibility horizon + gapless `seal_seq`, the
  transaction-scoped advisory lock, kill-read-from-writer-endpoint
  fail-closed, the shared kill/grant lock, the fail-loud topology
  interlock, per-role DB least privilege, the L4-only LB, the DB's
  Terraform on the agent-unreachable side, and the synchronous-standby
  kill-durability requirement.
- The velocity rail is explicitly re-characterized as best-effort under
  scale-out (or made exact via a serializable read) rather than silently
  weakened; the DOOR-scaling and DB-availability items are named as
  follow-on work rather than assumed solved.

## Alternatives considered

- **Vertical scaling + fast failover only** (one bigger VM, EBS-backed
  SQLite, automated restart). Cheapest, and genuinely enough for a lot
  of scale given the throughput finding — but it leaves a
  minutes-long availability gap on instance death and a hard vertical
  ceiling. Kept as the *small-tier* answer (it is essentially today's
  single VM); rejected as the *enterprise* answer because HA is the
  actual requirement.
- **Move the broker into the cluster as a Deployment** (the obvious
  "just make it a pod" instinct). Rejected without qualification — it
  ends the trust-domain separation ADR-004 exists to enforce
  (cluster-admin would reach the broker's trust domain). Horizontal
  scaling must happen *outside* the cluster, which the ASG + shared
  DB does.
- **A distributed/replicated SQLite (LiteFS/rqlite).** Rejected:
  it reintroduces multi-writer complexity to avoid a networked DB we
  already ship a chart for, and the audit hash chain's single-sealer
  model wants one authoritative store, not a replicated-consensus one.
- **Hard cutover to Postgres (drop SQLite).** Rejected: it taxes every
  small deployment with a DB dependency to serve the enterprise tier.
  Dual-support keeps "domain in, platform out" honest for the single
  VM.

## Sources (as verified 2026-08-24)

Repo, read at file:line by the recon: `sentinel/app/{service.py,
broker.py,main.py,door.py,audit_chain.py,models.py,db.py,config.py,
policy.py,ema.py}`, the Alembic migrations, `deploy/*.service`,
ADR-004 (trust rules, the "one droplet" line in context), ADR-010
(the named horizontal upgrade), CLAUDE.md (loopback/kill rules),
`cloud-cost-shape.md` (the bottleneck line, corrected here). Postgres
patterns (transaction-scoped `pg_try_advisory_xact_lock` singleton
sealer, the `pg_snapshot_xmin(pg_current_snapshot())` commit-visibility
horizon, JSONB, `SELECT ... FOR UPDATE`, `target_session_attrs=read-write`
+ `pg_is_in_recovery()` for writer-endpoint reads, synchronous Multi-AZ
standby vs async replica durability, RDS Proxy/pgbouncer transaction
pooling) are standard practice folded in from engineering knowledge —
each a well-established Postgres idiom, worth a confirmation pass at build
time. **This draft was revised by a three-lens adversarial review
(code-accuracy, trust-invariants, Postgres-correctness) on 2026-08-23**;
the review refuted the original wall-clock settle-window (Decision 3 now
uses the commit-visibility horizon) and added the fail-loud interlock,
per-role least privilege, agent-unreachable Terraform placement, L4-LB,
synchronous-standby, shared kill/grant lock, and velocity-rail caveats
recorded above.
