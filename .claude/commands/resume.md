You are resuming a long-running homelab platform engineering project. Follow the session protocol locked in `CLAUDE.md`. This is a learning project for the owner — teach as you go.

## Step 0 — Liveness gate (before trusting any doc)

Docs can claim "verified" while prod is broken — proven 2026-07-25:
every pod showed Running but chat was dead (k3s had rewritten the
CoreDNS NodeHosts configmap and severed the Ollama path). Run these
first and report PASS/FAIL on each line before reading further:

1. `docker ps` — k3d node containers + portainer up.
2. `kubectl get nodes` — 4 nodes Ready.
3. `kubectl get pods -A` — nothing CrashLooping or Pending.
4. LLM path from inside the cluster (deploy name is the current
   gateway — `litellm` until Phase 2.5 replaces it with `ai-gateway`):
   `kubectl exec -n chat deploy/litellm -- python3 -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434', timeout=5).read().decode())"`
   → must print "Ollama is running".
5. Ingress: `curl -s -o /dev/null -w '%{http_code}' -H 'Host: openwebui.lab.local' localhost:8080` → 200.

Any FAIL: diagnose and fix (or log to STATUS.md Backlog) before phase
work. A green gate is the entry criterion for every session.

## Step 1 — Orient

Read these files in order. Then state in 2 sentences where the project stands and what the next action is.

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
