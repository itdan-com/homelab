You are resuming a long-running homelab platform engineering project. Follow the session protocol locked in `CLAUDE.md`. This is a learning project for the owner — teach as you go.

## Step 0 — Liveness gate (before trusting any doc)

Docs can claim "verified" while prod is broken — proven 2026-07-25:
every pod showed Running but chat was dead (k3s had rewritten the
CoreDNS NodeHosts configmap and severed the Ollama path). Run these
first and report PASS/FAIL on each line before reading further:

1. `docker ps` — k3d node containers + portainer up.
2. `kubectl get nodes` — 4 nodes Ready.
3. `kubectl get pods -A` — nothing CrashLooping or Pending.
4. LLM path from inside the cluster, through the AI gateway with the
   real consumer key (reads OpenWebUI's own env — tests the exact
   production path: DNS → ExternalName → Envoy → auth → route):
   `kubectl exec -n chat deploy/openwebui -- python3 -c "import urllib.request, os, json; req = urllib.request.Request(os.environ['OPENAI_API_BASE_URL'] + '/models', headers={'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY']}); print([m['id'] for m in json.loads(urllib.request.urlopen(req, timeout=10).read())['data']])"`
   → must list `qwen3.5:9b`. If it fails, isolate the hop: swap the
   URL for `http://host.docker.internal:11434` with no auth header —
   "Ollama is running" means host path OK, gateway broken; a timeout
   means the host path itself is down. If the chain works but
   generation is glacially slow, check GPU contention from Windows
   games/apps: `powershell.exe nvidia-smi` — a game + the model can
   exceed the 4070's 12 GB and trigger the NVIDIA driver's silent
   system-RAM fallback (~0.05 tok/s; observed 2026-07-25).
5. Doors (TLS since Phase 5 A3; four since B7 — SSO is load-bearing,
   a dead Authentik locks every SSO login):
   `for h in openwebui authentik grafana argocd; do printf '%s: ' $h; curl -sk -o /dev/null -w '%{http_code}\n' -H "Host: $h.lab.local" https://localhost:8443/; done`
   → expected `openwebui: 200`, `authentik: 302`, `grafana: 302`,
   `argocd: 200` (the 302s are those apps' redirects to their own
   login flows — healthy; plain `http://:8080` answering 301 is the
   redirect middleware working, not a failure). If authentik is down,
   note: every app keeps a local break-glass login (see SETUP.md
   Part 2).
6. Sentinel — the trust anchor's units AND its transport (added
   2026-08-02: `Active: running` told the truth for five days while
   the console served plain http; check the wire, not the unit):
   `systemctl is-active sentinel-broker sentinel-admin` → both
   `active`; then
   `curl -sk -o /dev/null -w '%{http_code}\n' --max-time 5 https://localhost:8400/healthz`
   → `200` over httpS specifically. An http-only answer on 8400
   means the units are running stale code or a stale unit file —
   the fix is the install line in SETUP §1.7 (it restarts and
   wire-probes), not a manual restart.

Any FAIL: diagnose and fix (or log to STATUS.md Backlog) before phase
work. A green gate is the entry criterion for every session.

## Step 1 — Orient

Read these files in order. Then open with a kickoff statement that
**proves orientation without being asked** — the owner must never
have to say "do you know what we're working on?" It must name:

- **(a) The two flows, one line each:** Mission Control (the platform
  operates itself — the operator proposes PRs, the owner approves in
  GitHub as themselves, ArgoCD applies, `git revert` is the undo; no
  Slack, no Sentinel) and Airlock (the workforce reaches MCP tools —
  birthright entitlements by policy, dangerous power borrowed via
  time-boxed elevation through Sentinel/Cedar). The dividing line is
  revertibility.
- **(b) Exact position:** current phase, its next checklist item, and
  what that item actually does.
- **(c) Owner-paced pending items** from STATUS (name them once,
  no nagging).
- **(d) Anything broken, paused, or mid-flight** (e.g. a stopped
  timer, an open operator PR, a failed gate check).

If the docs left you unsure of ANYTHING, say so explicitly instead of
papering over it.

1. `CLAUDE.md` — architecture and principles
2. `STATUS.md` — current cursor (active phase, next action, recent activity log, backlog, blocks)
3. `docs/phases/phase-NN-*.md` — detailed checklist for the active phase named in `STATUS.md`

## Step 2 — Confirm

Tell me which checklist item we're about to tackle, any prerequisites, and any open questions noted in the phase doc. **Wait for my go-ahead before executing.**

## Step 3 — Execute, one item at a time

- One checklist item per round-trip. Do not batch multiple items without confirmation.
- Teach as you go — explain what each command does and why (this is a learning project; the owner is training to be a platform engineer).
- After each item: tick its checkbox in the phase doc; if anything surprising happened, jot a line under that phase doc's "Notes captured during execution" section.
- Verify the item's success criterion before moving on.

## Step 4 — End of session

Before the session closes, update state:

- `STATUS.md`: `Active phase`, `Status`, `Next action`, `Last updated` (use today's date).
- Prepend a one-line entry to `STATUS.md`'s `Recent activity log`.
- If anything noteworthy emerged that doesn't belong to the current phase, append it to `STATUS.md`'s `Backlog`.
- If a durable lesson, preference, or design decision emerged, save it as a memory file in `~/.claude/projects/-home-bob-homelab/memory/` and add an index entry to `MEMORY.md`.

## Rules of engagement

- **Security tripwires.** Any work involving Sentinel, MCP scoping, the kill switch, OAuth credentials, or any external SaaS access is security-sensitive per `CLAUDE.md`'s "Trust-domain separation" principle. Pause and explicitly confirm scope with the owner before proceeding.
- **No silent workarounds.** If a step fails, diagnose and explain. Don't bypass safety checks, hooks, or signing flags.
- **Doc disagreement.** If you find the phase doc is wrong, missing a step, or contradicts something in CLAUDE.md, surface it — we'll update the doc together rather than improvising around it.
- **Git literacy.** The owner is new to git and will not type git commands. Summarize any change in plain English first; the diff is supplementary.
- **Model.** Stay on the owner's configured default (Fable 5 as of 2026-07-25) unless told otherwise. Use the `Explore` subagent for any codebase search that would otherwise pull more than ~3 files into main context.

## Begin

Start with Step 1 now.
