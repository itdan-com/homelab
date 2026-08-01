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

- [x] Add a namespaced Role + RoleBinding in
      `k3d/operator-view-rbac.yaml` (the artifact bootstrap step 10
      applies): verb `get`, resource `services/proxy`, namespace
      `monitoring`, **`resourceNames` scoped to the one service** —
      list both `monitoring-kube-prometheus-prometheus` and the
      port-qualified `monitoring-kube-prometheus-prometheus:9090`
      (proxy authorization carries the `name:port` form).
- [x] Verify as the operator: the PromQL query above succeeds; a
      proxy GET to any OTHER service (e.g. ArgoCD's) stays
      Forbidden; the write and secret probes stay Forbidden.
- [x] Update `ops/operator/CLAUDE.md`: delete the dead exec
      instruction, document the proxy read path plus 2–3 canned
      PromQL queries (tokens/sec, per-pod CPU, current replica
      counts).
- [x] Probe gotcha to keep: `kubectl auth can-i get services/proxy`
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

- [x] **Idempotence:** open `operator/*` PRs counted from GitHub
      truth before any tokens are spent; the agent's context lists
      them and tick rule 2 forbids duplicating; a 60-min per-finding
      cooldown stops re-diagnosis of the same signal.
- [x] **Caps:** max 3 open operator PRs (script-enforced, before the
      model runs); per-finding cooldown after every agent pass,
      successful or not.
- [x] **Spend:** quiet ticks spend ZERO (deterministic watchman, no
      model at all — better than "a cheaper model"); anomaly passes
      on sonnet with cost + tokens logged per pass to
      observations.log; 20 agent passes/day ceiling; 15-min hard
      timeout. Dedicated key is 6.3.
- [x] **Fail-safe:** every tick starts with hard reset + clean of
      the operator clone; agent errors are logged verdicts (unit
      still succeeds); state records the pass even on failure so a
      broken agent cannot retry every 5 minutes.
- [x] Units in `ops/operator/deploy/`, installed by
      `install-tick.sh` — detect-at-install (checkout path, tool
      PATH); the committed units are host-agnostic (ADR-004).
- [x] Reboot posture: SETUP.md §1.8 — the timer runs while WSL is
      up (linger keeps it alive without a terminal); Windows still
      has to start WSL, and the doc says so plainly.

### 6.3 — State and scope the credentials exception

CLAUDE.md's rule is "Claude holds no long-lived external
credentials" — and the agent's own two are the stated exception: the
**Anthropic key** (to think) and the **GitHub App key** (to open
PRs) are what make the agent exist, not what it does; neither is
Sentinel-gateable. Living on a host rather than in a namespace is
the mitigation.

- [x] Write the exception into `ops/operator/CLAUDE.md` ("Your own
      credentials — the stated exception": what the two keys are, why
      Sentinel cannot gate them, what bounds the blast radius).
- [ ] Dedicated Anthropic key for the operator — **OWNER ACTION
      pending** (plumbing done): SETUP.md §1.8 has the three-step
      recipe (console workspace + spend limit + one env line); the
      next agent pass logs `auth=api-key` when it has taken effect.
      Until then agent passes log `auth=login` (the owner's).
- [x] Verify permissions: env/pem/kubeconfig were already 0600;
      directory tightened 755 → **700** (2026-08-01).
- [x] Rotation/revocation note in SETUP.md §1.8 (Anthropic console /
      GitHub App settings).

### 6.4 — The admin-bypass decision (gate hardening)

Planned since Phase 4.5: remove the repo-admin bypass from
`protect-main`. **Wrinkle found 2026-07-31:** the BUILDER's own doc
and chart commits are authored by the owner's account and land on
`main` through exactly that bypass. Straight removal deadlocks the
daily workflow — GitHub will not let the owner approve their own
PRs, and there is no second human. Swapping review-required for
status-checks-required is worse: the OPERATOR's PRs could then merge
with no human, destroying the gate this phase exists to keep.

- [x] Decide with the owner. **Decided 2026-08-01: (a) keep the
      bypass, loudly** — owner: "keep the bypass, that's for at the
      end, then we remove ourselves." Removal is end-state hardening
      for when no human pushes anymore; the ruleset-change alert
      lands with Phase 8's Alertmanager rules.
- [x] Record the decision and implement: recorded in SETUP.md §1.2
      step 3 (adopter-facing, with the deadlock reasoning);
      implementation is deliberately no ruleset change — that IS the
      decision.
- [x] Re-run the Phase 4.5 negative tests — done live on PR #7
      (App-authored, one doc line): App self-approve → **422** "Can
      not approve your own pull request"; App unreviewed merge →
      **405** "At least 1 approving review is required"; owner
      approved and squash-merged as themselves → OK. The gate holds
      by platform enforcement, not promises.

### 6.5 — Lifeline drill

A proposal that takes the cluster down must leave something alive to
propose the revert. The agent lives outside the cluster, so this
holds by construction — prove it anyway (probes assert settled
state; claims get drills).

- [x] With the cluster API unreachable (dead-port kubeconfig variant
      — zero cluster impact), the tick survived, flagged
      `api_unreachable`, and filed escalation issue **#8** through
      GitHub while its cluster view was dead — the lifeline holds.
      **Boundary finding:** the agent also diagnosed the drill (it
      read STATUS.md in its workspace) and *restored its own
      kubeconfig from the drill's `.bak`* — competent, and exactly
      the self-healing that inverts control in a real quarantine →
      **charter hard rule 7** now forbids repairing its own access.
- [x] Recovery: view restored (by the agent — see above), recovery
      tick green, timer resumed. Issue closure is HUMAN work by
      design (the agent reports; humans decide) — and structurally
      so today: the App token 403s on issue comments/edits, so the
      spec's "agent updates the issue on recovery" is impossible;
      permission gap tracked in STATUS backlog for 7.4.
- [x] No in-cluster dependency to think: the issue was filed while
      the cluster view was dead (GitHub + Anthropic external, QED);
      the envelope skipped cluster checks cleanly and still checked
      the doors host-side.

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
2. **Tick cadence + spend ceiling.** ~~5 min? Which model for quiet
   ticks? Per-tick and per-day token budgets?~~ **Decided at 6.2:**
   5-min timer; quiet ticks spend zero (no model); anomaly passes on
   sonnet; 3 open PRs / 60-min per-finding cooldown / 20 passes per
   day / 15-min timeout; measured pass cost $0.48–0.78. All knobs
   overridable in the operator env file.
3. **Agent memory across ticks.** ~~Stateless each tick vs a rolling
   observation log?~~ **Decided at 6.2:** the log — each pass sees
   the envelope findings, the open-PR list, and `tail -n 15` of
   `observations.log`. Proven working: pass 2 applied the recurrence
   threshold pass 1 had stated, because it could read pass 1's
   outcome.
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
- 2026-07-31 (6.1 DONE): battery green as the operator — `up` returns
  26 series; the scrape-targets API answers through the same path
  (extproc :1064 target up); same-namespace alertmanager proxy 403
  (proves resourceNames scoping, not just namespace); cross-namespace
  argocd proxy 403; create/exec/delete/secrets all still `no`. The
  token counters did not exist at first query — nobody had generated
  since the rebuild, and counters are born on first increment — so
  one 120-token completion was pushed through the production path:
  two series appeared on the next scrape and KEDA's exact
  ScaledObject query went from empty vector to computing values.
  Charter rewritten accordingly; "empty vector ≠ zero" recorded there
  for the future tick prompt.
- 2026-08-01 (6.2): two real bugs found by running, both fixed.
  (1) `view` RBAC cannot list nodes — the first envelope run printed
  `0/4 Ready` as *ok* with the Forbidden swallowed by /dev/null;
  fixed twice over: fewer-nodes-than-expected is now an anomaly
  (`nodes_missing`), and `operator-node-reader` ClusterRole grants
  get/list/watch. (2) The tick's readout piped JSON into
  `python3 - <<heredoc` — the heredoc replaces the pipe as stdin, so
  every pass logged PARSE-ERROR while the pass itself had succeeded;
  now parses the per-pass forensics file `last-result.json`. Also:
  `</dev/null` on the claude call (avoids a 3s stdin-sniff stall).
  **The agent's first two live passes behaved per charter:** pass 1
  concluded `ACTION: none` on a green forced wake and *declined* to
  act on the harness bug it noticed (out of tick scope), stating a
  recurrence threshold; pass 2 saw the recurrence in its observation
  history and filed issue #6 — correct escalation, closed same hour
  citing the fix commit (9545edf).
- 2026-08-01 (6.3): buildable parts done same day — charter
  exception section, dir 700, SETUP §1.8 key recipe + rotation, and
  agent log lines now carry `auth=api-key|login` so the key switch
  is observable. The key mint itself is the phase's one owner-console
  action; scheduled ticks kept firing green throughout the item.
- 2026-08-01 (6.4 gate retest): this line arrived via an
  App-authored PR. Before the owner touched it, the App attempted to
  approve its own PR (expect 422) and to merge it with zero reviews
  (expect 405) — results recorded in the PR conversation and in
  STATUS. The owner then approved and merged as themselves. The gate
  holds; author ≠ approver is enforced by the platform, not by
  promises.
- 2026-08-01 (6.5 drill debrief): the richest tick yet ($1.58, 38
  turns). The agent survived the cut, filed #8 via GitHub with its
  cluster view dead, initially misattributed the fault to a
  plausible-but-wrong `bootstrap.sh` corruption story, caught its own
  misframe after reading STATUS.md, tried to post a correction and
  discovered the App token cannot comment on issues (403) — a real
  permission gap. Buried in the wrong story was a REAL latent bug:
  bootstrap mints the operator kubeconfig from the ambient kubectl
  context (issue #8 stays open to track the fix). And it restored
  its own kubeconfig from the drill's backup — the boundary finding
  that became charter rule 7. Optional future: a drill marker file
  the tick prompt names, so game-day induced faults are reportable
  as such instead of inviting invented root causes.
