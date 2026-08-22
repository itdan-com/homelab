# ADR-006: Getting Sentinel's record into the observability stack

**Status:** **ACCEPTED-AS-BUILT** (2026-08-22, built under the standing
"Next action — implement ADR-006 (mine, no owner input needed)" in
STATUS.md). Originally Proposed 2026-08-04; **revised the same day
after owner review** — the first draft chose metrics-only and
justified the gap instead of closing it. See "What the first draft got
wrong", and the as-built addendum below for where the implementation
deviates from the text and what the adversarial review forced into
the open.

## As built (2026-08-22) — deviations and the exposure accounting, corrected

Recorded so the text above stays readable as the design and this
section as the truth:

1. **The bounded label is `event_type`, not "outcome".** The audit
   table's closed 11-value enum is the axis that exists; dashboards
   and alerts key on it. Same discipline, truer name.
2. **"Ship on seal" means per-row on the admin timer.** Sealing stamps
   hashes; segment FILES only exist at rotation — so the shipper
   pushes newly sealed rows every 30s tick in the segment LINE format
   (canonical + prev_hash + row_hash: Loki's copy carries the chain).
   The two synchronous seal calls on console endpoints do not ship;
   worst added lag is one tick.
3. **Rotation now refuses to outrun the copy**: `/v1/audit/rotate`
   409s rather than prune sealed rows the shipper has not shipped —
   without this, pruning unshipped rows would silently DROP the
   backlog gauge and mute the very divergence alert this ADR promised
   (review-caught).
4. **"No new secret to manage" was wrong.** Two new leaves
   (loki-client, prometheus-client) and two new out-of-git cluster
   Secrets (`sentinel-ca-clientauth`, `sentinel-prometheus-client` in
   `monitoring`), minted/injected by mint-certs.sh and now ALSO by
   bootstrap.sh (Prometheus hard-mounts the scrape secret — a rebuild
   without it bricks the whole metrics stack).
5. **"The one genuinely new exposure" is actually three, and the
   second needs an owner decision (#11).** (a) The push route, as
   designed — mTLS-gated, path-limited. (b) **prometheus-client in
   the monitoring namespace reaches EVERY broker route**, because the
   broker has no per-cert authorization: a compromised monitoring
   workload can file capability requests with attacker-authored text
   and poll/claim them. It cannot GRANT (a human passkey tap remains
   the only yes), reach the admin API, or touch the kill switch.
   Accepted-for-now with the narrowing named: a dedicated metrics
   listener validating a metrics-only sub-CA. (c) **Loki's in-cluster
   surface**: any pod can already push (forge) or query the audit
   COPY on :3100 — the mTLS gate covers only the outside door. The
   copy is confidentiality-expanded relative to the Sentinel-host
   original; a NetworkPolicy restricting :3100 to Alloy/Traefik/
   Grafana is the named follow-up (backlogged), and chain-verifying
   the copy against Sentinel is the eventual divergence check.
6. Two accepted read-surfaces stated plainly: `policy_version` as an
   info-label (one live series, console-minted content hash, never
   attacker text) and the Envoy fleet's proxy-client cert being able
   to read /metrics (counts and states only — no principals).

## Context

Phase 8 wants a dashboard answering *"who did what through the gate,
and how often does the platform say no?"* Every fact it needs exists:
Sentinel's audit table records principal, tool, resource, outcome,
policy version and timestamp for every decision, and 7.6 made that
record tamper-evident and exportable as JSONL.

The problem is location. **Sentinel runs outside the cluster**
(ADR-004, non-negotiable); Prometheus and Loki run inside it. Something
must cross that boundary, and the boundary is the point of the
architecture.

Constraints:

1. **The cluster must not gain a path to Sentinel's admin surface.**
   CLAUDE.md is absolute: Claude can be told *no* and can observe
   denials, but cannot reach the API that grants.
2. **Loki has no authentication** (`auth_enabled: false`). Anything
   that can reach its push endpoint can write logs, including forged
   ones. An audit record an attacker can append to is not one.
3. **Sentinel already calls INTO the cluster** on every forwarded tool
   call. That direction is established; the forbidden direction is
   cluster → Sentinel's admin API.

## What the first draft got wrong

It proposed metrics only, and argued the per-event view was already
served by the console. Both halves were wrong in the same way:

- **"The console can answer it" is true and irrelevant.** Operators
  look at Grafana. A capability that lives only in a second tool is a
  capability most people will not use, and at 3am nobody remembers
  which surface holds which half of the story. Correlating *"Alice
  called `github.create_pull_request` at 14:22"* with what
  `github-mcp` logged at 14:22 is one query if both are in Loki and a
  manual timestamp comparison across two systems otherwise.
- **It treated Loki's missing auth as a reason to stop rather than a
  thing to design.** The auth problem is real and small; the gap it
  was used to justify is neither.

And it missed the argument that actually settles the question:

> **Exporting the audit record to a separate system makes tampering
> harder, not easier.** Today the canonical record lives in one SQLite
> file on one host. Whoever can edit that file can edit history — the
> 7.6 hash chain makes it *detectable*, which is exactly as far as a
> single-copy record can go. A copy living in a system with a
> different administrator and a different attack surface means erasing
> history requires compromising both. That is a stronger property than
> anything achievable on one machine, and it is the property auditors
> are actually asking about when they ask for centralised logging.

## Decision

**Both, for different questions.**

### 1. Metrics on Sentinel's broker listener — for rates and state

A read-only `/metrics` endpoint on the **broker** (mTLS, cluster
already holds a client certificate, grants nothing). Aggregates with
**bounded labels only** — outcome, server, grant state. No principal,
no resource: those are unbounded and belong in the log, and putting
them in labels is the cardinality mistake this phase already warned
about.

This answers: how many denials, how many elevations are live, is the
kill switch on, which policy version is deciding. Alertable, cheap,
real-time.

### 2. Audit segments shipped to Loki — for "what did Alice do"

Sentinel's sealed JSONL segments (built in 7.6 for exactly this) are
pushed to Loki, **through an authenticated path**:

- A Traefik IngressRoute exposing **only** Loki's push endpoint
  (`/loki/api/v1/push`) — never its query API, which stays in-cluster.
- **mTLS using Sentinel's own CA**, which already exists and already
  mints client certificates for precisely this kind of "prove you are
  the component you claim" problem. Same pattern as the proxy's
  `proxy-client` certificate: holding a certificate from Sentinel's CA
  is the price of writing to that endpoint.
- No new secret to manage — the PKI is already there and already
  rotates via `mint-certs.sh`.

Labels stay bounded (`source="sentinel"`, `outcome`, `server`); the
principal, resource and policy version live **in the line**, where
Loki's search finds them without exploding the index.

**Timing note, stated because it changes what the dashboard can
promise:** segments are sealed and rotated on a schedule, so shipping
them lags. For live per-event visibility, ship on *seal* rather than
on rotation — a small change to the sealing loop, and the honest
alternative to pretending a forensic export is a live feed.

## Consequences

- Two paths out of Sentinel, both read-only, neither touching the
  admin API: metrics over the existing mTLS listener, logs over an
  authenticated push route.
- Loki's push endpoint becomes reachable from outside the cluster —
  **the one genuinely new exposure here**, and the reason it is
  mTLS-gated and path-limited rather than simply ingressed.
- The Airlock dashboard can show both rates and individual events, so
  one surface answers both questions.
- The canonical record stays in Sentinel with its hash chain; Loki
  holds a copy for querying and correlation. Divergence between the
  two is itself a signal worth alerting on later.
- Cost: an ingress route, a client certificate, and a shipping path.
  Roughly a session, and the auth question is answered before it is
  built rather than alongside.
