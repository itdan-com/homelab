# ADR-009: Surviving the substrate — GitHub outage posture for Mission Control

**Status:** **Proposed** (2026-08-22). Drafted at owner request after a
four-agent assessment of the owner's question: *"what do we do about
mission control's single point of failure when github goes down?"*
Provenance, stated precisely: D2/D4/D5's *direction* was pre-agreed
in chat the same day (owner: *"i tink github unreachable makes sense,
break glass makes sense and slscak"* — quotes committed to STATUS's
activity log so this claim is checkable); D3 was recommended and the
owner asked *"you think a mirror is good?"* — answered yes, not yet
formally confirmed; D6 wraps the owner's own idea (*"a local github
instance in the cloud with the stack could almost make sense"* — a
musing, promoted to a designed-but-deferred decision by this ADR).
Formal accept/amend at next kickoff makes all of it binding.

**Cloud context, recorded at the owner's word:** *"the end state is
getting this into cloud via a terraform script that just pushes this
to like aws k8s i think."* Everything this ADR builds is a cloud
artifact per ADR-002 — the mirror timer and tick changes are written
for a cloud VM and merely run on WSL2 today — and the AWS lean is
recorded against owner-decision #3 (Phase 9 target), where the
cost-shape doc's k3s-on-VMs finding gets re-presented before
committing.

## Context — what the assessment established

Full findings with file:line detail live in STATUS.md's 2026-08-22
backlog entry; the load-bearing facts:

- **A GitHub outage freezes change; it does not take the platform
  down.** ArgoCD is not in the data path. Apps are expected to flip
  to sync-status Unknown roughly 3-6 minutes in — inferred from the
  documented 3-minute branch→SHA cache plus the controller's 180s
  repo-error grace period, wire-checked by D4's probe pass — and
  once Unknown, **selfHeal stops enforcing** (maintainer statement
  in argo-cd discussion #21072, corroborated by the auto_sync docs'
  error-state rule; the T+10m edits-stick probe in D4 confirms it on
  our wire). Running workloads never notice.
- **Sentinel is fully independent** — its policy store is a local git
  repo with no remote (ADR-005 D5; policy.py:396-400). Kill switch,
  audit, elevation, and every non-GitHub MCP server keep working.
  The trust-domain separation paid for itself here unprompted.
- **Mission Control dies first, and it takes the watchman with it.**
  launch.sh:39-43 mints a GitHub App token and fetches the repo under
  `set -euo pipefail` at the top of every tick — before
  envelope-check.sh (whose own header says "NO GitHub token") ever
  runs. GitHub-down is an unlogged abort: no envelope check, no
  heartbeat line, no alert; the interactive mode is gated by the same
  block. Contrast: Anthropic-down is a *logged* `agent-error`
  verdict. Every escalation path (PR, issue) rides GitHub; no second
  channel exists.
- **The repo-server wgets its SOPS toolchain from github.com on every
  pod start** (catalog/argocd/values.yaml:197-206) — a restart
  mid-outage sticks in Init and ArgoCD can render nothing. Subchart
  tarballs are deliberately gitignored; 4 of the 8 charts' lockfiles
  resolve to `*.github.io` repos (argoproj, kedacore, grafana ×2,
  prometheus-community); and the live cluster runs at least six
  distinct ghcr.io image repos — three pinned in catalog values plus
  authentik's and KEDA's subchart defaults: a **rebuild during a
  GitHub outage is impossible today**.
- **The dangerous moment is recovery, not the outage.** Break-glass
  kubectl edits stick while apps are Unknown, then get silently
  reverted by selfHeal at GitHub's first successful refresh — unless
  auto-sync was paused first and the changes committed before
  re-enabling.
- **No mirror exists anywhere.** One remote; the operator's clone
  points at the same GitHub URL; the in-cluster deploy key is
  deliberately read-only (that unchecked write box is a security
  property this ADR does not touch).
- **Sizing reality:** git operations are GitHub's least-affected
  surface (~19 incidents touching them in the last measured year vs
  57 for Actions — a third-party aggregator's count, directionally
  sound, not GitHub's own); the one total git outage in GitHub's own
  availability reports ran 64 minutes (2025-11-18). PR-surface
  degradation is more common than git-push death.

## Decision 1 — The posture: wait it out, but never blind

"Wait it out" is the default for the change pipeline: an hour-scale
outage of a rare kind does not justify operating our own
high-availability forge. Two things are NOT acceptable to wait out, and
they are what this ADR builds: losing the platform's eyes (D2), and
losing the ability to rebuild or to act in an emergency (D3, D4).

Two principles bound everything below:

- **The gate fails closed for the agent, open for the human.** A
  GitHub outage must never escalate Mission Control into a
  direct-apply path — the approval gate disappearing is precisely
  when the agent must not act. Break-glass (D4) is a *human*
  procedure on a channel that does not ride GitHub.
- **Degrade loudly, never silently.** Every GitHub-down behavior this
  ADR touches converts an invisible failure into a logged fact — and
  the *page* comes from one named mechanism that can actually fire:
  D5's in-cluster alert rule on ArgoCD apps entering sync-Unknown,
  which is the cluster-side symptom of exactly this outage. (An
  exit-0 "logged verdict" cannot trigger systemd's OnFailure=; the
  first draft promised alerting with no mechanism — caught in
  review, fixed by naming the real one.)

## Decision 2 — The tick degrades instead of aborting

`ops/operator/launch.sh` and `bin/gh-app-token.sh`, concretely:

1. **The envelope check runs FIRST.** It needs no GitHub anything —
   kubeconfig and the local door origin only, per its own header; it
   moves ahead of the token mint. The platform keeps its 5-minute
   watchman through any GitHub outage.
2. **`github_unreachable` becomes a logged verdict, not a crash —
   and refusal stays loud.** If the token mint or fetch fails on a
   *network*-shaped error (timeout, DNS, 5xx), the tick writes one
   line to observations.log — `verdict=github_unreachable` plus the
   envelope summary it just gathered — and exits 0, exactly the
   shape the model-failure path already has (launch.sh:158-187 logs
   `agent-error` and exits 0; the asymmetry was in the code, not the
   design). But an HTTP 401/403 from a *reachable* GitHub is a
   different animal: gh-app-token.sh's own header documents App
   uninstall as the owner-side kill switch, and a deliberate cut
   must stay loud and distinct — that path logs
   `verdict=github_auth_refused` and exits NON-zero so OnFailure=
   fires. Revocation pages a human; it never blends into outage
   noise.
3. **Timeouts on every GitHub call — all three of them.** `curl -m
   10` (and `--retry` as appropriate) on gh-app-token.sh:33-41 and
   the launch.sh:40 fetch, plus the tick's third GitHub call the
   first draft missed: the `gh pr list` open-PR cap at
   launch.sh:123 (wrapped in `timeout`, its failure handled as
   github_unreachable) — so a GitHub blackhole starting mid-tick
   cannot wedge the unit against its 900s TimeoutStartSec at any of
   the three call sites.
4. **The agent knows mid-pass.** GitHub reachability joins the
   envelope output as a reported *note* — context the agent sees
   when something ELSE wakes it — not a wake-triggering anomaly on
   its own (an agent woken by github_unreachable alone can neither
   PR nor issue and has nothing to diagnose; the deterministic path
   already logged it). A pass already running when GitHub dies
   reports "GitHub unreachable" instead of authoring a PR into a
   void — the owner's own framing, adopted verbatim. The tick prompt
   gains the vocabulary: an ACTION line variant for "found
   something, could not report to GitHub — logged locally." To be
   exact about who writes what: the agent *emits* that ACTION line
   on stdout and the harness writes it to observations.log exactly
   as it does the other three variants — the agent gains no write
   path to the state directory (hard rule 7's read-only list is
   untouched).
5. **Interactive mode starts degraded rather than refusing** —
   read-only cluster look with a loud "GitHub unreachable" banner, so
   the owner can always hand-drive the operator's eyes.
6. **Unit crashes get a credential-free failure hook.** `OnFailure=`
   on operator-tick.service pointing at a small handler that writes
   a loud local marker — and holds NO webhook, NO external
   credential. The first draft put the Slack webhook in this script;
   review correctly killed it: a webhook readable by the operator's
   user is a third long-lived external credential in the agent's own
   trust domain, an ungated non-PR capability (ADR-001's amended
   rule), and a way to spoof or drown the alert channel — and
   CLAUDE.md already says Slack belongs to Airlock, not Mission
   Control. The *push* notification for GitHub-down comes from D5's
   in-cluster rule instead; OnFailure covers genuine unit crashes
   (including D2.2's loud github_auth_refused exit) in the local
   record.

Charter notes, all listed in build item 1: hard rule 7 ("your own
access is not yours to repair") gains one sentence — when the
reporting channel itself is the broken thing, the report is the local
log line (via the ACTION contract above), and the agent still repairs
nothing. The same carve extends to the other two places that command
the impossible during an outage — tick-prompt rule 4 ("open or
update ONE GitHub issue instead") and charter rule 6 ("say so and
open an issue") — each gaining "…unless GitHub itself is the
unreachable thing, in which case the logged-locally ACTION line IS
the report." A headless model mid-outage must hold one consistent
instruction, not three contradictory ones.

## Decision 3 — A mirror is insurance, not a service

A **bare mirror** on the operator host: one systemd timer fetching
from GitHub into a local bare repo every few minutes. No web UI, no
accounts, nothing to operate. Its three jobs: the rebuild has a
provider-independent source; ArgoCD has a documented repoint target
for a long outage (`spec.source.repoURL` edits work live — expect the
stale-history "not our ref" wrinkle, discussion #25455, cleared by a
sync or redis restart); and a future forge (D6) seeds from it.
Verification is part of the artifact: the timer's health check clones
back from the mirror and diffs refs — a cron mirror that rots
silently is worse than no mirror, because it gets trusted.

Same decision, same theme — **the cluster stops fetching its own
tooling from GitHub at runtime:**

- The repo-server initContainer stops wget-ing sops/helm-secrets on
  every pod start: vendor the two binaries. The once-built image is
  the ADR-002-compliant artifact; a hostPath the initContainer
  copies from is the *named local-only variant* (hostPath is exactly
  the k3s-ism the portability audit flags), acceptable in the lab
  only with that label. This is also a supply-chain fix: today those
  are unpinned-by-hash binaries fetched at runtime.
- The rebuild's chart-tarball exposure (4 of 8 Helm repos are GitHub
  Pages; `charts/` deliberately gitignored per bootstrap.sh:148-150)
  gets the cheapest fix that preserves that decision: the mirror
  timer also refreshes a local chart-tarball cache the bootstrap can
  fall back to. Committing the tarballs (reversing the gitignore
  decision) is the named alternative if the cache proves fiddly.

Placement honesty (review-caught): "the operator host" is today
**Sentinel's host** — STATUS owner-decision #6's named interim
deviation. So D2's OnFailure handler and D3's mirror timer land on
the kill switch's host until Phase 9 separates the VMs. Named here,
not silent; both artifacts are credential-free precisely so this
placement adds nothing to what that deviation already risks.

## Decision 4 — The break-glass runbook (probe first, then write)

One page in `docs/operator-cheatsheet.md`, Google-SRE-shaped: the
glass is noisy, the gate is bypassed loudly, and the outage window is
reconciled afterwards — post-hoc review replaces pre-hoc review only
for that window.

The spine, from verified ArgoCD semantics:

1. **Pause first.** Before any manual fix: disable auto-sync — the
   Application CR lives in-cluster, so `kubectl patch` works with
   GitHub down (`spec.syncPolicy.automated.enabled=false`), or an
   AppProject deny sync-window with `manualSync: true` as the
   cleanest cluster-wide primitive.
2. **Fix imperatively, log loudly.** kubectl, recorded in
   observations.log and (once D5 lands) Slack.
3. **Commit before re-enabling.** On recovery, the manual changes go
   into git FIRST; only then does auto-sync come back — otherwise
   selfHeal silently reverts the hotfix at the first refresh. One
   correction from review, because this repo has NO app-of-apps
   root (the argocd chart is not ArgoCD-self-managed): the
   **ApplicationSet controller re-stamps child syncPolicy as soon as
   its git generator can reach the repo again**, ungated by any
   per-Application pause — so the primitive that survives recovery
   is the AppProject deny window (which blocks syncs regardless of
   what policy gets stamped back) or scaling the
   applicationset-controller down for the window. Which, exactly, is
   a probe question below.

The runbook is written only AFTER a live probe pass on devlab (block
github.com via a CoreDNS override) covering the FULL set the
research mandated, not a subset: measure the actual fight window and
confirm edits stick at T+10m (the ~3-minute inference needs a wire
check); watch the status progression AND confirm Health stays green
throughout; confirm our installed ArgoCD honors `automated.enabled`
and carries `--repo-error-grace-period-seconds` (both
version-dependent); **confirm the AppProject deny sync-window takes
effect with git down** — it is the runbook's recommended primitive
and must not be the unprobed one; **delete the repo-server pod, then
the redis pod, mid-outage** and confirm the immediate-Unknown
degradation and non-recovery-until-git-returns (this changes the
runbook's timing model); rehearse the mirror repoint (expect the
stale-history wrinkle); confirm the re-entry revert with an
uncommitted edit in place; and time when the ApplicationSet re-stamp
lands after git returns with a patched child in place. The probe
doubles as the drill — this is the GitHub-side twin of 6.5's
lifeline drill, which only ever tested the cluster-side direction.

## Decision 5 — The alert channel is the one already owed, plus one new rule

The non-GitHub push channel is **Phase 8's existing Alertmanager →
Slack webhook item** (owner-paced, needs the webhook URL) — the
webhook lives in-cluster in Alertmanager's config, never on the
operator host (see D2.6's review finding). What this ADR adds is the
**detector**: one Alertmanager rule on ArgoCD applications entering
sync-status Unknown — the in-cluster symptom of exactly this outage,
visible to Prometheus via the argocd metrics Phase 8 already scrapes.
That rule is the mechanism behind D1's "degrade loudly": it fires for
a GitHub outage without the operator host holding any credential, and
it also catches every other way ArgoCD loses its source. Until the
webhook exists, the honest statement stays: alerts fire into
Alertmanager's UI and observations.log only.

## Decision 6 — The forge ships with the stack (designed now, built in cloud)

The owner's idea, adopted as product direction: *"a local github
instance in the cloud with the stack could almost make sense, and it
auto pushing to the repo after."* This is not an outage spare — it is
promoting the forge to a first-class citizen of the product, the same
sentence as ADR-005 D9(c)'s "self-hosted MCP servers are product
scope": compliance buyers get a GitOps loop that runs entirely on
infrastructure they deploy, with github.com optional.

Designed constraints, so the build session inherits decisions instead
of questions:

- **The forge gets its own trust domain — a fourth VM is the
  default.** GitHub is not just our git host — it is the trust
  domain of Mission Control's approval gate: the agent authors, a
  human approves with an identity neither the agent nor the cluster
  can touch. A forge inside the cluster it governs collapses that
  (cluster-admin merges the agent's own PRs — the same shape as
  ADR-004's cluster-admin-becomes-kill-switch-admin rejection) and
  dies with the thing it rebuilds. So: its own small VM, the
  Sentinel pattern applied again. Cloud end-state topology: cluster
  + Sentinel VM + operator VM + forge VM. Never on Sentinel's host —
  that is CLAUDE.md's agent-loopback reasoning extended to the
  forge's admin surface. Co-locating forge with *operator* is the
  named cost-reduction option, and review forced its true cost into
  print: it puts the gate's refs, merge state, and admin plane on
  the agent's own host, surrendering host-level author≠approver — so
  it is acceptable ONLY with the forge's data and admin plane
  unreachable from the operator's user (separate system user, no
  shared writable paths, admin bound away from anything the agent
  reaches), and the fourth VM remains the default.
- **The gate's invariant ships as configuration-as-code, verified by
  a negative test.** GitHub enforces "no identity approves its own
  PR" as a platform invariant; on Gitea/Forgejo that property is
  admin-droppable configuration. The forge therefore ships branch
  protection equivalent to the current protect-main ruleset —
  required review, author's own approval rejected, admin bypass
  logged — as code, and the deployment's battery includes the
  negative test: the agent's App identity attempting to approve or
  merge its own PR must fail. Without this constraint the whole
  design is a gate drawn on paper.
- **Sync is one-way, forge → github.com, as a push mirror.** Gitea
  has this built in (Forgejo's equivalent to be verified in the
  Phase 9 session). Two-way sync is rejected permanently —
  divergence resolution is where mirror schemes die.
- **ArgoCD points at the forge** in that shape; github.com becomes
  the public copy. The forge's availability becomes ours to own —
  that is the honest cost, and why this waits for cloud rather than
  landing in the lab: GitHub's uptime beats anything we run on one
  VM, so promoting the forge only pays when self-containment is the
  product feature being sold.
- **Not built now.** Phase 9 (Terraform, the owner's stated AWS
  lean) is where this lands, as its own module. D3's bare mirror is
  its seed and is not throwaway work.

## Build plan

One hardening session (the **ADR-009 session**), slotted after
Phase 8's ADR-006 build, before or interleaved with Phase 7.8 at the
owner's choice:

1. D2 — the tick surgery + the three charter/prompt carves (hard
   rule 7, charter rule 6, tick-prompt rule 4) + the ACTION-line
   vocabulary (tests: a tick against a blackholed GitHub produces
   the logged verdict AND a completed envelope check; a 401/403
   from a reachable stub produces github_auth_refused and a failed
   unit).
2. D3 — mirror timer + clone-back verification + initContainer
   vendoring + chart-tarball cache.
3. D4 — the devlab probe pass, then the runbook page.
4. D5 — the ArgoCD sync-Unknown Alertmanager rule is buildable NOW
   (it joins the six live rules); only its Slack delivery rides the
   existing owner-paced webhook item.
5. D6 builds nothing now; its constraints above are its deliverable,
   plus a Phase 9 pointer in `docs/phases/phase-09-cloud.md`.

## Non-goals

- A warm standby forge for outage-time PR review (Gitea pull mirrors
  cannot take PRs — go-gitea#6054; conversion is one-way; for
  hour-scale outages, review-over-the-mirror with post-hoc PR
  reconstruction is the honest gate).
- Agent proposal-queueing machinery (no established pattern was
  found across several search angles — absence of search evidence,
  not verified absence; once D3's mirror is live, queued proposal
  branches are nearly free to add if ever wanted — deliberately not
  now).
- Two-way git sync, ever.
- Touching the in-cluster deploy key's read-only property.

## Sources (dated, as verified 2026-08-22)

Repo: `ops/operator/{launch.sh,bin/gh-app-token.sh,bin/envelope-check.sh,CLAUDE.md,tick-prompt.md,deploy/operator-tick.service}`,
`catalog/argocd/{values.yaml,templates/applicationset.yaml}`,
`bootstrap.sh`, `sentinel/app/policy.py`, ADR-002, ADR-004, ADR-005
D5/D9. External: argo-cd docs (auto_sync, sync windows, HA cache
TTLs, controller/repo-server flags) and maintainer statement in
discussion #21072 (Unknown state enforces nothing) + #25455 (repoURL
repoint history wrinkle); GitHub availability report Nov 2025 (the
64-minute git outage) and the GitHub Online Services SLA;
go-gitea#6054 (mirrors take no PRs); Gitea repo-mirror docs +
GitLab push-mirror docs (the push-mirror mechanics); Google SRE book
(noisy glass, reconcile afterwards — the "post-hoc review replaces
pre-hoc for the window" framing is GitOps practitioner writeups,
community-report, not the SRE book); mirror-hygiene rationale
(clone-back verification, --mirror force-push footgun) is
community-report (practitioner blogs + GitHub roadmap #478);
IncidentHub aggregate outage counts (vendor-claim, methodology
counts all status-page entries); Flux contrast (drift correction
continues from stored artifact — architecture-derived) noted as the
worse failure mode for break-glass. Per-service incident split (git
ops least-affected) is vendor-claim from the same aggregator,
directionally sound.
