# Phase 6 — Mission Control complete

**Goal:** graduate the Phase 4.5 operator from "proposes PRs when a
human launches it" to "proposes PRs because it noticed something": a
continuous agent that watches Prometheus and the Kubernetes API from
OUTSIDE the cluster, whose every external action is a pull request
with a plain-English summary, approved by the owner in GitHub as
themselves.

**What this phase is NOT (re-cut 2026-07-28; doc rewritten
2026-07-31):** no Slack, no MCP servers, no SaaS, and therefore **no
Sentinel involvement** — Mission Control is PR-only by construction,
and ADR-001's amended rule permits PR-only work without capability
gating. The PR review is the whole gate; `git revert` is the undo.
The moment a wanted action is not a PR, that feature belongs to
Airlock (`phase-07-airlock.md`), not here. The previous version of
this file described the pre-re-cut phase (in-cluster agent, Slack ✅
buttons, MCP catalog, Sentinel gating, five ADR-005 blockers); all of
it MOVED to Phase 7 — nothing was deleted. See `CLAUDE.md` → "Two
flows: Mission Control and Airlock".

**Status:** Not started. Entry criteria met: Phase 5.5 closed
2026-07-28; the PR loop itself has worked since Phase 4.5.

---

## The properties this phase must end with (from CLAUDE.md)

1. The agent runs **outside the cluster** it operates — it must
   survive the thing it repairs, or `git revert` has nobody left to
   propose it.
2. It does **not share a host with Sentinel** — a process on that
   host can reach the loopback-bound admin surface. **Violated
   today** (open question 1): `launch.sh` and the operator clone
   live on the WSL2 host, which is Sentinel's host.
3. It observes **Prometheus and the Kubernetes API** through a real,
   authorized read path. Not Loki — that is Phase 8; no log-driven
   proposals this phase.
4. Its every external action is a **pull request** — plain-English
   summary first, diff second. Nothing else.
5. **A human approves in GitHub, as themselves.** No bot merge, no ✅
   button, no auto-merge for any tier.

## Checklist

### 6.1 — Real Prometheus read path (kill the exec hack)

Facts verified 2026-07-31 with the operator's own kubeconfig:

- The charter's documented fallback (`kubectl exec` into the
  OpenWebUI pod) **was never executable**: `auth can-i create pods
  --subresource=exec` → `no` under `view` RBAC. The operator has
  never had working metrics access at all.
- The clean path is denied **only by RBAC**: `get` on
  `services/proxy` → `no` for the operator, while the same
  API-server proxy URL under admin credentials returns Prometheus
  data fine:
  `/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090/proxy/api/v1/query?query=up`.

Work:

- [ ] Add a namespaced Role + RoleBinding in
      `k3d/operator-view-rbac.yaml` (the artifact bootstrap step 10
      applies): verb `get`, resource `services/proxy`, namespace
      `monitoring`, **`resourceNames` scoped to the one service** —
      list both `monitoring-kube-prometheus-prometheus` and the
      port-qualified `monitoring-kube-prometheus-prometheus:9090`
      (proxy authorization carries the `name:port` form).
- [ ] Verify as the operator: the PromQL query above succeeds; a
      proxy GET to any OTHER service (e.g. ArgoCD's) stays
      Forbidden; the write and secret probes stay Forbidden.
- [ ] Update `ops/operator/CLAUDE.md`: delete the dead exec
      instruction, document the proxy read path plus 2–3 canned
      PromQL queries (tokens/sec, per-pod CPU, current replica
      counts).
- [ ] Probe gotcha to keep: `kubectl auth can-i get services/proxy`
      parses `proxy` as a service NAME (`view` can read any service →
      false "yes"); the honest probe is `--subresource=proxy`, and
      the honest-er probe is the live call.

Why this shape: the query rides the operator's existing identity
through the API server — authenticated, audited, revocable in the
same RBAC file, zero new network surface — and works identically
against any managed Kubernetes (ADR-002 parity). A Prometheus door
would violate the exposure policy (doors are for browsed,
authenticated apps); port-forward needs `pods/portforward` create
and a tunnel per tick.

### 6.2 — The tick: continuous observation loop

Make the operator run unprompted. Recommended shape (decide at
start): a **systemd user timer** on the operator's host firing every
~5 min, running a headless pass (`claude -p` with the charter and a
tick prompt) via a new `launch.sh --tick` mode that keeps
`--strict-mcp-config` with the deliberately empty MCP config.

Each tick: read cluster state + Prometheus → compare against known
envelopes → either append one heartbeat line to a local observation
log or open **exactly one PR per concern**.

Guards that must exist before the timer goes live:

- [ ] **Idempotence:** query open `operator/*` PRs first; an
      already-proposed concern is a heartbeat, not a duplicate PR.
- [ ] **Caps:** max open operator PRs (propose 3); cooldown per
      concern after a PR is opened or closed-unmerged (a rejected
      proposal is an answer, not an invitation to retry).
- [ ] **Spend:** its own Anthropic key (6.3); a cheaper model for
      quiet ticks; tokens-per-tick logged. Per-day ceiling decided
      at open question 2.
- [ ] **Fail-safe:** a tick that errors leaves nothing half-done
      (PR-or-nothing), and the next tick reports the failure in its
      heartbeat.
- [ ] Timer + service files live in `ops/operator/` and follow the
      cloud-artifact rule: the same files a cloud VM would run; an
      install step detects host-specific values; no lab-isms inside
      the units.
- [ ] Reboot posture documented with Sentinel's honesty (SETUP
      §1.7): Windows does not start WSL2, so "continuous" means
      "while the lab host is up" — say so plainly.

### 6.3 — State and scope the credentials exception

CLAUDE.md's rule is "Claude holds no long-lived external
credentials" — and the agent's own two are the stated exception: the
**Anthropic key** (to think) and the **GitHub App key** (to open
PRs) are what make the agent exist, not what it does; neither is
Sentinel-gateable. Living on a host rather than in a namespace is
the mitigation.

- [ ] Write the exception into `ops/operator/CLAUDE.md`.
- [ ] Dedicated Anthropic key for the operator (not the owner's
      personal key), spend-capped in the Anthropic console.
- [ ] Verify `~/.config/homelab-operator/env` and the App private
      key are 0600 and owned by the operator's user.
- [ ] One-line rotation/revocation note in SETUP.md (Anthropic
      console / GitHub App settings).

### 6.4 — The admin-bypass decision (gate hardening)

Planned since Phase 4.5: remove the repo-admin bypass from
`protect-main`. **Wrinkle found 2026-07-31:** the BUILDER's own doc
and chart commits are authored by the owner's account and land on
`main` through exactly that bypass. Straight removal deadlocks the
daily workflow — GitHub will not let the owner approve their own
PRs, and there is no second human. Swapping review-required for
status-checks-required is worse: the OPERATOR's PRs could then merge
with no human, destroying the gate this phase exists to keep.

- [ ] Decide with the owner. Honest options: **(a)** keep the
      bypass, loudly — it stays the builder's write path, every use
      is visible in the push log, and the ruleset-change alert lands
      with Phase 8's Alertmanager rules; revisit when a second human
      or a PR-shaped builder exists (recommended). **(b)** remove it
      — dead until a second approving identity exists. **(c)** a
      second human account as reviewer — not real today.
- [ ] Record the decision (mini-ADR or charter note) and implement.
- [ ] Re-run the Phase 4.5 negative tests: App self-approve → 422;
      review-less merge → 405; owner-approves-App-PR path merges.

### 6.5 — Lifeline drill

A proposal that takes the cluster down must leave something alive to
propose the revert. The agent lives outside the cluster, so this
holds by construction — prove it anyway (probes assert settled
state; claims get drills).

- [ ] With the cluster API unreachable (stop the k3d node
      containers, or point the kubeconfig at a dead port —
      reversible), one tick must: not crash, detect unreachability,
      and still exercise its GitHub path — file the charter's
      escalation issue ("cluster API unreachable since <t>").
- [ ] Restart the cluster; the next tick returns to heartbeat and
      updates/closes the issue.
- [ ] Confirm no tick step depends on an in-cluster service to
      think: Anthropic and GitHub are external; the Prometheus path
      fails soft.

### 6.6 — Demo + phase close

- [ ] Drive the Phase 3 k6 burst; within one tick interval the agent
      opens an unprompted scaling PR (KEDA ceiling or warm-spare
      floor) with the charter's plain-English body.
- [ ] Owner approves and merges **in GitHub, as themselves** →
      ArgoCD applies → scale event visible in Grafana.
- [ ] Capture the GIF (demo asset per milestone; README).
- [ ] STATUS.md updated; activity log entry; backlog sweep for
      anything this phase surfaced.

## Open questions (resolve during the phase)

1. **Where does the agent live? (owner call)** Today: the WSL2 host
   — which is Sentinel's host, violating property 2. Mitigations in
   place: Sentinel's admin API requires a WebAuthn session (reaching
   loopback ≠ approving); Sentinel state is owned by the `sentinel`
   user; the operator runs as `bob`. Options: **(a)** accept as a
   NAMED interim deviation until Phase 9 gives it its own VM
   (ADR-004 already names that cloud shape) — recommended; **(b)**
   the Mac (it sleeps — weakens "continuous"); **(c)** a dedicated
   device/VM now. Recording (a) also means correcting CLAUDE.md's
   "today that is the owner's workstation" line at the next
   architecture edit.
2. **Tick cadence + spend ceiling.** 5 min? Which model for quiet
   ticks? Per-tick and per-day token budgets?
3. **Agent memory across ticks.** Stateless each tick vs a rolling
   observation log the tick reads and appends (recommended: the log —
   continuity without conversation state).
4. **Event-driven wake.** An Alertmanager webhook → host would add
   an inbound cluster→host surface. v1 is polling-only; revisit at
   Phase 8 with the alert rules.

## Phase exit criteria

- A timer runs the operator tick unattended; quiet ticks heartbeat;
  the exec hack is gone from the charter; the proxy path is the only
  metrics access and is scoped to one service.
- An induced load signal produces one well-formed unprompted PR
  within one tick interval; duplicate signals do not duplicate PRs;
  caps and cooldowns hold.
- The owner approves and merges in GitHub as themselves; ArgoCD
  applies; no bot merge, no auto-merge anywhere.
- Lifeline drill passed: a tick with a dead cluster survives and
  files the escalation issue; the recovery tick returns to
  heartbeat.
- Credentials exception stated in the charter; the operator has its
  own spend-capped Anthropic key; key files 0600.
- Admin-bypass decision recorded and implemented; the 4.5 negative
  tests re-pass (422 / 405 / owner-path merges).
- Zero Slack, MCP, SaaS, or Sentinel touchpoints in the loop
  (`--strict-mcp-config` with the empty config, verified).
- STATUS.md updated; demo GIF captured.

## Notes captured during execution

- 2026-07-31 (doc re-cut session): verified with the operator's own
  kubeconfig that the charter's `kubectl exec` metrics fallback was
  never executable (`pods/exec` denied), and that the API-server
  proxy path to Prometheus works under admin but is RBAC-denied for
  the operator — 6.1 is one scoped Role away.
