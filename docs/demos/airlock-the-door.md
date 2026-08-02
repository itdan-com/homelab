# Demo — Airlock: one address, your own identity, borrowed power

**Phase 7.3.** What this shows: a person points an MCP client at one
address, signs in with the company identity, and immediately has the
tools their role should have. Consequential tools are **borrowed** for a
window with one deliberate act, never held. Nobody sees what they have
no business seeing.

The number this retires: **Phase 5.5.8 measured seven human approvals
for a single MCP session.** Through the door, the same session costs
**zero**, and one write window costs **one**.

---

## Before you start (5 minutes, once)

**1 — Install the door.** From the repo, in your own terminal:

```bash
cd ~/homelab/sentinel && sudo ./scripts/install-systemd.sh
```

It installs a third unit (`sentinel-door`), then **probes the wire**:
the door must answer https, publish MCP discovery whose advertised
address matches the origin you type, refuse an unauthenticated call
with a pointer to sign-in, and agree with the broker on the active
policy version. If any of that is wrong the install stops there.

**2 — Add yourself to the policy store.** This is the step that
surprises everyone, and it is the architecture working: signing in
proves *who* you are; the policy store alone decides *what* you may do.
A valid sign-in for someone the store has never heard of lists **zero**
tools.

On the console (`https://localhost:8400`) → **Access → People → add**:

| field | value |
|---|---|
| email | `bob@itdan.com` — the address on your Authentik account |
| groups | `engineering` (in the example store: echo read + github write-on-request) |

Save & activate. Watch the version change.

---

## The demo

### 1. One address, and it refuses strangers

```bash
curl -sk https://localhost:8402/.well-known/oauth-protected-resource
curl -sk -X POST https://localhost:8402/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The first prints the RFC 9728 document that tells a client where to sign
in. The second is a **401 with a `WWW-Authenticate` header pointing at
it** — the machine-readable version of "you need to sign in first."

### 2. Sign in from a real MCP client

```bash
export NODE_EXTRA_CA_CERTS=/etc/sentinel/certs/ca.crt
claude mcp add --transport http airlock https://localhost:8402/mcp
claude mcp login airlock
```

A browser opens on your company sign-in. Sign in as yourself. That is
**the only time a human acts** in this whole demo until the elevation.

Worth noticing in the URL bar before you sign in: the client sent
`client_id=https://claude.ai/oauth/claude-code-client-metadata` — the
client's identity **is a URL** (CIMD, the MCP spec's replacement for
dynamic registration). The door fetched that document, checked it, and
matched its redirect. There is no registration endpoint here at all;
self-registration is refused permanently.

### 3. Your tools are there, and only yours

In a Claude Code session, ask what tools it has. You will see the
`echo.*` and `github.*` tools your group entitles — **and no
`hr-platform.*` at all**. They are not listed-and-refused; they are
absent. Approvals so far: **zero**.

### 4. Borrowing power

Ask it to do something consequential — open a pull request
(`github.create_pull_request`). The call comes back refused, but the
refusal is the product: it names what borrowing would take and hands
back a **one-time link**.

Open the link. It asks you to sign in (the model cannot follow it — it
holds an API token, not a browser session; this is deliberate, because a
model that can elevate itself is the hole this architecture exists to
close). Then: **Unlock `github:write` for 30 / 60 / 120 minutes?**

Click 30. Go back and run the call again — it works, and keeps working
for that window, for you, closing by itself. **Total human acts: one.**

### 5. What "forbidden" means

The example store forbids writes to `hr-platform` on the **prod** tier
for everyone, at every rung. There is no window, no approval, and no
button — while the same tool on **staging** simply asks for approval.
That distinction is a property of the resource, not of the person.

### 6. The record

Console → **Borrowed right now** shows the live window with a **Revoke**
button. The audit log carries the grant (with who issued it, via which
door, for how long) and every call made inside it, each stamped with the
policy version that decided it.

---

## What is honest about this demo

- **The tool behind the demo is a stand-in.** The door forwards
  allowed calls through the same `sentinel-proxy` every in-cluster
  caller uses — it does not go around the enforcement point — but the
  server on the other end is the 5.5 mock, not a system anyone
  cares about. The first real one is 7.4 (the owner's on-prem
  GitHub). A server with no upstream configured says so plainly
  instead of pretending the call worked.
- **One human is both requester and approver here.** The `approve`
  rung files a card for a *different* person; with one enrolled
  passkey that person is you. The mechanism (console grant, window,
  `granted_by`) is enforced regardless, and the collapse disappears
  when a second passkey enrols.
- **`localhost:8402`, not `mcp.<domain>`.** The door binds loopback in
  the lab. Serving real workstations is two env values
  (`SENTINEL_DOOR_BIND`, `SENTINEL_DOOR_ORIGIN`) plus a
  publicly-trusted certificate — which is exactly what the cloud phase
  provisions.
