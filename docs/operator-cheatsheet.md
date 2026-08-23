# Operator Cheatsheet

A copy-paste-friendly guide to the things you'll most often want to do
in this homelab. UI-first; CLI fallback shown for cases where Portainer
or the browser falls short. Designed for an owner who reads diffs but
does not type git.

---

## Access URLs

| Thing | URL | Notes |
|---|---|---|
| **Authentik (SSO — the front door)** | `https://authentik.lab.local:8443` | Needs hosts entry `127.0.0.1 authentik.lab.local`. **Sign in with your Authentik account** — the portal shows one-click tiles for OpenWebUI/Grafana/ArgoCD. Admin console: `akadmin` (password: `sops -d catalog/authentik/secrets.enc.yaml` → `bootstrap_password`), "Admin interface" button top-right. Onboard a teammate: Directory → Users → create + set password → add to `*-users`/`*-admins` groups. |
| **OpenWebUI (chat)** | `https://openwebui.lab.local:8443` | SSO: "Continue with Authentik" (or the tile). Roles: `openwebui-admins` group = app admin, `openwebui-users` = user; non-members are denied at Authentik. Break-glass: the pre-SSO local signup (first local account = admin). Pick model **qwen3.5**. Hosts entry below; http :8080 redirects. |
| **ArgoCD (GitOps UI)** | `https://argocd.lab.local:8443` | SSO: "LOG IN VIA AUTHENTIK" (or the tile). `argocd-admins` = full admin, `argocd-users` = read-only. Break-glass: `admin` / `kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' \| base64 -d`. Eight green tiles = platform matches git. Port-forward fallback: `kubectl port-forward -n argocd svc/argocd-server 8081:80`. |
| **Grafana (dashboards)** | `https://grafana.lab.local:8443` | SSO: "Sign in with Authentik" (or the tile). `grafana-admins` = org Admin, `grafana-users` = Viewer. Break-glass: `admin` / `sops -d catalog/monitoring/secrets.enc.yaml`. Start with "AI Gateway — Token-Rate Autoscaling". Fallback: `kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80`. |
| Portainer | `https://localhost:9443` | From Windows browser. Self-signed cert → click "Advanced → Proceed". |
| Portainer (from Mac) | `https://<windows-LAN-ip>:9443` | Get the Windows IP via `ipconfig` in PowerShell. |
| AI Gateway (API only) | `http://ai-gateway/v1` — in-cluster only | OpenAI-compatible LLM gateway (Envoy AI Gateway). Needs a per-consumer Bearer key; deliberately NOT reachable from a browser — OpenWebUI is the only user-facing door. |
| k3d cluster API | `https://0.0.0.0:39093` | Only from inside WSL. `kubectl` already knows this. |

**Applying `catalog/argocd` changes** (the one chart ArgoCD does NOT
manage — self-management deferred in Phase 4): after editing it, run
`helm secrets upgrade argocd catalog/argocd -n argocd -f catalog/argocd/values.yaml -f catalog/argocd/secrets.enc.yaml`
then `kubectl -n argocd rollout restart deploy/argocd-server` if
`argocd-cm`/`argocd-secret` changed. Every other chart applies itself
via git push + ArgoCD sync.

If you forgot the Portainer admin password, the only recovery is to
restart the container with the password-reset flag (ask Claude to do
this — one command).

### Reaching `*.lab.local` services in a browser

The `openwebui.lab.local`-style names route through the cluster's
ingress (Traefik). Two things make them work:

1. **A hosts entry** on whichever machine's browser you're using. On the
   Windows box, edit `C:\Windows\System32\drivers\etc\hosts` — open
   Notepad via **right-click → Run as administrator** first, or it won't
   save — and add:
   ```
   127.0.0.1 openwebui.lab.local
   ```
2. **The port is `:8080`.** k3d maps the WSL2 host's port 8080 to the
   cluster ingress (and 8443 → HTTPS, once cert-manager lands in Phase
   5). So the URL is always `https://<name>.lab.local:8443` — plain
   `http://<name>.lab.local:8080` still works but 301-redirects there
   (Phase 5 A3).

**No-hosts-file fallback** (quick test, bypasses ingress — tunnels
straight to the Service):
```
kubectl port-forward -n chat svc/openwebui 8888:8080
```
then browse `http://localhost:8888`.

**From the Mac (not the Windows box):** WSL2 ports aren't on your LAN by
default. Until that's wired up (Windows `netsh portproxy`, or WSL2
mirrored networking — see STATUS backlog), use the Windows box's browser
via RDP.

---

## "Is everything healthy?" 30-second check

In a normal WSL terminal:

```
docker ps
kubectl get nodes
kubectl get pods --all-namespaces
```

What you want to see:
- `docker ps` shows **portainer** plus several **k3d-devlab-…** containers, all `Up`.
- `kubectl get nodes` shows **4 nodes** all in `STATUS: Ready`.
- `kubectl get pods --all-namespaces` shows everything in `Status: Running` (a `Completed` job is fine). `CrashLoopBackOff` or `ImagePullBackOff` = something is wrong; copy the line and ask Claude.

**Visual version of the same check (no terminal):**
1. Open Portainer.
2. Click **devlab** environment.
3. Left sidebar → **Cluster** → see 4 nodes green.
4. Left sidebar → **Applications** → all rows show `Healthy`.

---

## I want to look at logs for some app

**In Portainer:**
1. **devlab** environment → **Applications** in left sidebar.
2. Click the app (e.g. `openwebui`).
3. **Pods** tab → click the pod name.
4. **Logs** tab.

**In a terminal (faster sometimes):**
```
kubectl get pods -n <namespace>
kubectl logs -n <namespace> <pod-name> -f
```
(The `-f` flag streams new lines as they come in. Ctrl-C to stop.)

---

## Restart a misbehaving deployment

**In Portainer:**
1. **Applications** → click the app.
2. Top-right **"Redeploy"** button.

**In a terminal:**
```
kubectl rollout restart deployment/<name> -n <namespace>
```

This kills the pods one at a time and lets the Deployment controller
recreate them with the same image and config. Use this when an app is
in a weird state but you have no recent change to blame.

---

## "I rebooted WSL / Windows. What now?"

When WSL restarts, all containers are stopped by default — including
Portainer and the k3d cluster nodes. They come back as containers but
the cluster's networking/state has to re-stabilize.

**Quick recovery (in a WSL terminal):**
```
docker start portainer
k3d cluster start devlab
```

Wait ~30 seconds, then do the health check above. If the host alias
broke (because WSL's IP shifted), the symptom is: pods can't reach
`host.docker.internal` anymore. Fix: the mapping lives declaratively in
`k3d/coredns-custom.yaml` — update the IP there, `kubectl apply` it,
restart CoreDNS (ask Claude). No cluster recreate needed.

---

## Operator PRs — how to read a diff (live since Phase 4.5)

When the operator proposes a change, **GitHub emails you** about a
new pull request from `itdan-homelab-operator[bot]`. The body always
leads with a plain-English summary (charter template), the diff
follows. *(There is no Slack and no ✅ button for infra changes — a
deliberate 2026-07-28 decision: "changes for the entire infra are too
big to approve via a slack button, even a misclick." Slack arrives
with Airlock, Phase 7, for people-facing tools only.)*

**The 30-second decision protocol:**
1. Open the PR. Read the **What & why** paragraph first.
2. Click **Files changed** — `+` is added, `-` is removed.
3. Ask yourself: **does the diff match the English summary?**
   - If yes → Review changes → **Approve**, then **Merge**.
   - If the diff shows *more* changes than the summary mentions →
     close it with a comment.
   - If you don't recognize the file path → close it and ask.
4. If unsure, close it. Merging applies within ~1 min (ArgoCD);
   closing is free — the operator treats a closed PR as an answer,
   not an invitation to retry (cooldown enforced).
5. Undo is always `git revert` of the merge — ask any session.

**Common safe diffs (✅ usually fine):**
- Single integer change: `replicas: 2` → `replicas: 4`
- Image tag bump: `image: foo:v1.2.0` → `image: foo:v1.2.1`
- Resource request/limit tune: `memory: 512Mi` → `memory: 1Gi`

**Diffs to scrutinize harder (✅ only after reading carefully):**
- Anything touching `clusterRole`, `clusterRoleBinding`, `rbac` —
  permissions changes.
- Anything in `kube-system`, `sentinel`, or `platform-control`
  namespaces.
- `NetworkPolicy` changes — affect what can talk to what.
- New `Secret` references — verify the secret name is one you
  recognize.

**Diffs to almost always reject (❌):**
- Bundles touching > 3 unrelated files. Make Claude split it up.
- Anything adding a credential, key, or token to a non-secret resource.
- Anything that removes a NetworkPolicy or RBAC binding without a
  written reason in the PR description.

---

## "Claude is doing something I don't like — STOP"

Three layers, smallest hammer first:

**Stop the operator's scheduled ticks** (the continuous agent — it
runs on the HOST, not in a pod):
```
systemctl --user stop operator-tick.timer
```
Restart later with `start`. An in-flight pass can only open PRs and
issues anyway — nothing merges without you.

**Kill all MCP capability grants** (Sentinel, live since Phase 5.5):
open the Sentinel console (`https://localhost:8400/`, passkey) and
press the **global kill switch** — every outstanding token dies,
new requests are refused until you release it.

**Stop the whole cluster:**
```
k3d cluster stop devlab
```
Cluster is paused; nothing in it can do anything until `k3d cluster
start devlab`. State on the persistent volumes is preserved. (The
operator survives this on purpose — that's its lifeline — but all it
can do about it is file an issue.)

---

## The operator tick (Mission Control, Phase 6)

The platform watches itself every 5 minutes. A deterministic script
checks seven envelopes (nodes, pods, ArgoCD, KEDA ceiling, token
rate, doors, API) — green costs zero tokens; a tripped check wakes a
headless Claude pass that may open at most one PR or issue per
finding. Guards: 3 open PRs max, 60-min per-finding cooldown, 20
passes/day, 15-min timeout.

```
tail -f ~/.config/homelab-operator/observations.log   # watch it live
systemctl --user list-timers 'operator-tick*'         # is it armed?
systemctl --user stop|start operator-tick.timer       # pause/resume
```

Tripwire table + the first-flight test record:
`docs/demos/mission-control-three-declines.md`. Install/knobs:
SETUP.md §1.8. Reboot note: the timer runs while WSL is up.

## Storage / disk space

WSL2's virtual disk grows on demand but does not shrink automatically.
If you've been creating and destroying lots of clusters and the WSL
disk feels huge:

**See what's eating disk inside WSL:**
```
df -h /
du -sh /var/lib/docker
docker system df
```

**Clean up unused Docker stuff (safe — only removes things nothing
references):**
```
docker system prune -a --volumes
```

Warning: `--volumes` will delete the `portainer_data` volume too if
Portainer isn't running. Start Portainer first, then prune.

---

## TLS: the lab CA (Phase 5)

The platform signs its own `*.lab.local` certificates. The trust
anchor is a 10-year CA managed by cert-manager:

- **Where it lives:** Secret `lab-local-ca` in namespace
  `cert-manager` (keypair), created by the `lab-local-ca` Certificate
  in `catalog/cert-manager/`. The `lab-local-ca` **ClusterIssuer** is
  what every chart's Certificate references.
- **Leaf certs renew themselves** (cert-manager default ~90 days,
  re-issued ~30 days early). Nothing to do, ever. Only the CA is
  special.
- **Export the CA cert for a client machine** (the file you import
  into a trust store):
  ```
  kubectl get secret lab-local-ca -n cert-manager \
    -o jsonpath='{.data.ca\.crt}' | base64 -d > lab-local-ca.crt
  ```
- **Check CA expiry:**
  ```
  kubectl get certificate lab-local-ca -n cert-manager \
    -o jsonpath='{.status.notAfter}'
  ```
- **Rotation = planned migration, not maintenance.** The CA's private
  key is pinned (`rotationPolicy: Never`); auto-renewal fires once a
  decade and any rotation changes what clients trust. To rotate
  deliberately: delete the `lab-local-ca` Secret, let cert-manager
  re-issue (new key), then **re-import the new `ca.crt` on every
  client machine** (list of clients: SETUP.md). Every leaf cert
  re-issues automatically afterward.
- **Something won't get a cert?** `kubectl describe certificate
  <name> -n <ns>` — the Events section says why; then `kubectl get
  clusterissuer lab-local-ca` (READY must be True).

---

## kubectl quick reference (read-only operations)

You probably won't type these often, but if Claude tells you to "run
this to check X," these are the patterns you'll see most:

| Want | Command |
|---|---|
| All pods, every namespace | `kubectl get pods -A` |
| Pods in one namespace | `kubectl get pods -n <ns>` |
| Detail on one pod | `kubectl describe pod <name> -n <ns>` |
| Logs from a pod | `kubectl logs <name> -n <ns>` |
| Logs, follow new lines | `kubectl logs <name> -n <ns> -f` |
| Shell into a pod | `kubectl exec -it <name> -n <ns> -- sh` |
| All services in cluster | `kubectl get svc -A` |
| Recent cluster events | `kubectl get events -A --sort-by=.lastTimestamp \| tail -20` |
| Save a manifest to a file | `kubectl get <kind> <name> -n <ns> -o yaml > x.yaml` |

---

## "Just give me a shell on the WSL host"

From Windows:
1. Open Windows Terminal (or any terminal that knows about WSL).
2. Type `wsl` and press Enter.
3. You're now in `~`, as user `bob`.

From the Mac (via RDP):
1. RDP into the Windows box.
2. Open Windows Terminal there.
3. `wsl`.

---

## Cluster lifecycle reference

| I want to | Command |
|---|---|
| Pause the cluster (preserve state) | `k3d cluster stop devlab` |
| Resume the cluster | `k3d cluster start devlab` |
| **Destroy and recreate** the cluster | `k3d cluster delete devlab && ./bootstrap.sh` — that's the whole runbook (proven 2026-07-27) |
| Verify a rebuild (or any day) | `/resume` liveness gate, then `scripts/sso-dance.sh` (7 asserts) + `scripts/netpol-smoke.sh` (3 asserts) |
| List clusters k3d knows about | `k3d cluster list` |
| Get an admin kubeconfig file | `k3d kubeconfig get devlab` |

**A rebuild is safe but not free.** Proven by the 2026-07-27 DR
drill: everything reassembles from git via `bootstrap.sh` — Cilium,
the lab CA (from `k3d/lab-ca.enc.yaml`, so browsers keep trusting),
all 10 catalog apps, SSO, the operator kubeconfig, Portainer's
network attachment. Survives on host paths: postgres (Authentik
users/groups), OpenWebUI data, `~/homelab-data`. **Dies with the
cluster:** Prometheus metrics history and hand-made Grafana tweaks
(local-path PVCs — dashboards re-provision, history doesn't), plus
the ArgoCD local `admin` break-glass password (regenerated; read it
back with the command in SETUP.md Part 2). Take a fresh backup first
anyway: `kubectl exec -n chat postgres-0 -- pg_dumpall -U postgres > backup.sql`.

---

## Where things live on disk

| What | Path |
|---|---|
| This repo / project files | `~/homelab/` |
| kubeconfig (admin creds!) | `~/.kube/config` |
| k3d cluster state | inside the container volumes (managed by k3d) |
| Portainer config + DB | Docker named volume `portainer_data` |
| Docker images | `/var/lib/docker/` (don't poke directly) |
| Phase docs | `~/homelab/docs/phases/` |
| This cheatsheet | `~/homelab/docs/operator-cheatsheet.md` |

---

## When something looks wrong but you're not sure

In order:
1. Run the **30-second health check** at the top of this page.
2. Look at recent cluster events: `kubectl get events -A --sort-by=.lastTimestamp | tail -30`
3. If still unclear, paste the symptom and the events output into a
   Claude session. Don't change anything until Claude has read the
   state.
4. Worst case: `k3d cluster stop devlab` to freeze the situation, then
   investigate.

## When GitHub is down (ADR-009 — drilled live 2026-08-22, every number below observed)

**What it looks like:** ArgoCD apps flip to sync-status `Unknown`
starting ~7 minutes in, staggered per-app (all 15 by ~11 min). Health
stays green; running workloads never notice. The
`ArgoCDApplicationSyncUnknown` alert reaches **Firing ~12 minutes in**
— that page is how you find out. The operator tick keeps running its
envelope every 5 minutes and logs `verdict=github_unreachable` with
the full cluster picture; it cannot open PRs or issues and correctly
does not try. (If the log says `github_auth_refused` instead, GitHub
is UP and refusing the App — that's the revocation kill switch's
shape; check the GitHub App before anything else.)

**Default response: wait.** Full git outages typically run about an
hour. Change is frozen; nothing is broken.

**Break-glass (only when a fix truly cannot wait):**

1. **Pause syncing FIRST — with the AppProject deny window, not a
   per-app patch:**
   ```
   kubectl patch appproject default -n argocd --type merge -p \
     '{"spec":{"syncWindows":[{"kind":"deny","schedule":"* * * * *","duration":"24h","applications":["*"],"manualSync":true}]}}'
   ```
   Why first: in the opening minutes of an outage selfHeal still
   enforces from warm caches — a hand edit at T+69s was reverted in
   under 20 seconds. Why the window and not
   `automated.enabled=false` on the app: the ApplicationSet
   controller **re-stamped the per-app pause 2.5 minutes after
   GitHub returned**; the deny window survived and was the only
   thing keeping the edit alive.
2. **Fix imperatively** (`kubectl scale/edit/...`). Note what you did
   somewhere durable.
3. **On recovery, order is everything: commit the manual change to
   git BEFORE lifting the window.** Observed: an uncommitted edit
   died 20 seconds after the window came off. Lift the window last:
   ```
   kubectl patch appproject default -n argocd --type json -p \
     '[{"op":"remove","path":"/spec/syncWindows"}]'
   ```

**Do not delete ArgoCD pods during an outage.** The repo-server's
init step fetches tooling from github.com and a mid-outage restart
sticks in `Init:1/2` until GitHub returns (observed: ~5 minutes stuck,
recovered 44s after DNS came back).

**Rebuild during an outage:** the mirror has everything —
`git clone file://$HOME/.local/state/homelab-mirror/repo.git`, with
the catalog's chart tarballs and the sops/helm-secrets artifacts
cached beside it (`charts/`, `tools/`). `last-sync.txt` tells you how
fresh it is; the mirror timer refreshes and clone-back-verifies every
10 minutes.
