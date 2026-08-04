# ADR-006: Getting Sentinel's record into the observability stack

**Status:** Proposed (2026-08-04). **Revised the same day after owner
review** — the first draft chose metrics-only and justified the gap
instead of closing it. See "What the first draft got wrong".

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
