# Demo — a person opens a real pull request through Airlock

**Phase 7.4.** Concretely: what to type, what comes back, what to
click, and how to know it worked. Nothing abstract.

**What it proves:** a person signs in with the company identity, sees
only the tools their role allows, is refused a consequential one,
borrows the power for a fixed window with one deliberate act, and the
work lands in GitHub — with the whole thing recorded.

---

## 0. Preconditions (all already true on this host)

- `sentinel-broker`, `sentinel-admin`, `sentinel-door` active
- `github-mcp` pod Running in `mcp-servers`
- a **GitHub App** credential saved in the console (Access →
  Connections → `github`)
- **Discover tools** run once, so the store carries the real tool names
- your email is a **person** in the store, in a group with access to
  `github`
- your MCP client is connected: `claude mcp list` shows
  `airlock … ✔ Connected`

Check the first three in one line:

```bash
systemctl is-active sentinel-broker sentinel-admin sentinel-door
kubectl -n mcp-servers get pods -l app.kubernetes.io/name=github-mcp
curl -sk https://localhost:8402/healthz     # note the policy_version
```

---

## 1. Ask for something real

A pull request needs a branch with a commit on it, so this is three
tools, not one — which is the point: **one elevation covers the whole
task**, not one approval per call.

In a Claude Code session that has `airlock` connected, say:

> Using the airlock tools, in `itdan-com/homelab`: create a branch
> called `airlock-demo` from `main`, add a file
> `docs/demos/airlock-proof.md` containing one line saying this came
> through Airlock, then open a pull request from `airlock-demo` into
> `main` titled "Airlock: first governed pull request".

## 2. What comes back the first time

The first write call is refused, and the refusal carries the fix:

```
MCP error -32003: This tool needs approval from a different person.
Open this link to request it: https://localhost:8402/elevate/<one-time-token>
```

If your group has `github` at *write — self-approve, timed* instead,
the wording is **"a timed elevation you can start yourself"** and step
3 is shorter (you pick a window, no console visit).

The agent **should not** open that link. It holds an API token, not a
browser session, and following it would make the model the second
human — which is the whole point of putting elevation behind a browser.

## 3. Borrow the power

Open the link yourself.

**If it says "Ask for approval on `github:write`?"** — click **Request
approval**. That files a card on the Sentinel console. Go to
`https://localhost:8400/`, sign in, find the pending request, and click
**Grant**.

> **Watch the window.** The console's Grant buttons are **5m / 1h**,
> and the button wins over whatever the request asked for. Pick **1h**
> unless you want to watch it expire — a known rough edge (STATUS
> backlog: the card should default to the requested window).

**If it says "Unlock `github:write` for 30 / 60 / 120 minutes?"** —
click a window. Done, no console visit.

## 4. Ask again

Same request, same session. This time all three calls go through and
you get a PR number back.

Confirm it exists — from anywhere:

```bash
gh pr list --repo itdan-com/homelab
```

The branch and file are real, the PR is real, and it was created by the
**GitHub App**, not by you personally (see "What the logs say").

## 5. What the logs say

Console → **Borrowed right now**: the live grant, its profile, who
issued it, and a **Revoke** button.

Console → the audit log carries, for this one task:

| event | what it records |
|---|---|
| `auth_success` | you signed in at the door, and from which client |
| `denial` | the refused call, with `outcome=approve`, the derived resource `github/itdan-com/homelab`, and the policy version that decided it |
| `request` → `grant` | the elevation asked for and granted, with `granted_via=approve` and the approver's name |
| `use` × 3 | each call inside the window, with its tool and resource |
| `use` (door-upstream) | the HTTP status GitHub returned, and how long it took |

**The honest limit:** GitHub's own audit log attributes all of it to
the App, because there is no "on behalf of" header in the protocol.
**Sentinel's log is the only place that knows which human acted.** The
fix is per-person GitHub tokens (`ghu_`), which this server already
accepts — it changes where the door gets the token, nothing else.

---

## When it doesn't work

| symptom | cause | fix |
|---|---|---|
| tools list is empty | you are signed in but not a **person** in the policy store | console → Access → People → add your email to a group |
| `unclassified-tool` | the store does not know that verb | console → Connections → **Discover tools** |
| `unmapped-resource` | the call left out `owner` or `repo` | ask again naming the repo as `owner/repo` |
| "no upstream is configured" | no connection saved for `github` | console → Access → Connections |
| "the enforcement proxy refused this call" | policy allowed it, the proxy did not — a platform fault | check `journalctl -u sentinel-door` and the broker's audit rows |
| GitHub returns 403 | the App lacks a permission, or is not installed on that repo | GitHub → your App → Install / permissions |
| PR creation fails with "no commits between" | the branch has no commit ahead of `main` | the file-creation step did not run; do it before the PR |
