# ADR-006: Getting Sentinel's record into the observability stack

**Status:** Proposed (2026-08-04). Decision needed before Phase 8's
"Airlock activity" dashboard can exist.

## Context

Phase 8 wants a dashboard answering *"who did what through the gate,
and how often does the platform say no?"* Every fact it needs already
exists — Sentinel's audit table records principal, tool, resource,
outcome, policy version and timestamp for every decision, and 7.6 made
that record tamper-evident and exportable as JSONL.

The problem is location, not data. **Sentinel runs outside the
cluster** (ADR-004, non-negotiable), and both Prometheus and Loki run
inside it. Something has to cross that boundary, and the boundary is
the entire point of the architecture — so "just expose it" is not
available.

Three constraints frame every option:

1. **The cluster must not gain a path to Sentinel's admin surface.**
   CLAUDE.md is absolute on this: Claude can be told *no* by Sentinel
   and can observe denials, but cannot reach the API that grants.
2. **Loki has no authentication.** `auth_enabled: false`, single
   tenant. Anything that can reach its push endpoint can write logs —
   including forged ones. An audit record that an attacker can append
   to is not an audit record.
3. **Sentinel already calls INTO the cluster** (the proxy, on every
   forwarded tool call). That direction is established and safe; the
   forbidden direction is cluster → Sentinel's admin API.

## Options

### A. Sentinel pushes to Loki

Sentinel POSTs audit rows to Loki's push API as it seals them.

- Direction is correct (Sentinel → cluster, already established).
- Needs a network path: Loki is ClusterIP, so this means an ingress —
  **and that ingress would accept unauthenticated writes from anything
  else that can reach it.** Fixable with basic auth or mTLS at the
  ingress, which is real work and a new secret to manage.
- Couples Sentinel's hot path to Loki's availability unless carefully
  made async.

### B. Prometheus scrapes a `/metrics` endpoint on Sentinel

Sentinel exposes counters — decisions by outcome, live grants, kill
state, policy version — and Prometheus scrapes it as a static target,
exactly as it would any host exporter.

- **Direction:** cluster → Sentinel. Allowed ONLY because the metrics
  endpoint grants nothing: it is read-only, exposes no capability, and
  would live on the broker's mTLS listener, which the cluster's Envoy
  already holds a client certificate for. **This is not a path to the
  admin API**; it is a new read-only surface on a listener the cluster
  can already reach.
- Gives real-time dashboards and alerting (denial rate, kill flips)
  with no new secret and no new ingress.
- **Cannot carry per-event detail.** Prometheus is for aggregates; a
  metric labelled with every principal and resource is exactly the
  unbounded-cardinality mistake Phase 8 already warned about.

### C. Alloy on the Sentinel host ships the JSONL segments

A small Alloy systemd unit tails `/var/lib/sentinel/audit-segments/`
and pushes to Loki.

- Correct direction, and the segments are already the export format —
  7.6 built them for exactly this.
- Same Loki-has-no-auth problem as A.
- Adds a second always-on process to the trust anchor's host, which is
  the machine we most want to keep boring.
- Only sees SEALED, ROTATED segments — so it lags by the retention
  window. Useless for "what just happened", fine for forensics.

## Decision

**Take B now, and defer A/C until there is a reason.**

The dashboard people actually want is *rates and outcomes* — how many
denials, how many elevations, is the kill switch on, which policy
version is deciding. Every one of those is a counter, and counters are
what Prometheus is for. It needs no new ingress, no new secret, no
weakening of Loki, and no second process on Sentinel's host.

The per-event view — *"show me the twelve calls Alice made inside her
elevation window"* — is a legitimate need and is **already served**:
it is the console's audit view, reading the canonical record directly,
which is the correct place for it. Copying those rows into Loki so a
second tool can show them worse is not an improvement.

Revisit if either becomes true:
- someone needs to correlate Sentinel events with cluster logs *in one
  query* (then C, with authenticated push), or
- the audit record must survive the host's loss in real time rather
  than at rotation (then A, and the auth question must be answered
  first, not alongside).

## Consequences

- Sentinel gains a `/metrics` endpoint on the **broker** listener
  (mTLS, cluster-reachable, read-only). Not on the admin listener,
  which stays loopback-only, and not on the door, which faces people.
- Metrics are **aggregates only, with bounded labels**: outcome
  (permit/confirm/approve/forbid), server, and grant state. **No
  principal, no resource** — those are unbounded and belong in the
  audit log, which already has them.
- Prometheus gets a static scrape target for the host, the same
  address the proxy already uses.
- The Airlock dashboard is then rates and states; the console remains
  the place to read a specific person's history.
- **Named honestly:** this means the Grafana view cannot answer "what
  did Alice do" — by design, not by omission. Two tools, two
  questions, and the audit log stays the single source of truth for
  the second.
