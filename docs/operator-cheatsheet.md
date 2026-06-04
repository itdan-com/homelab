# Operator Cheatsheet

A copy-paste-friendly guide to the things you'll most often want to do
in this homelab. UI-first; CLI fallback shown for cases where Portainer
or the browser falls short. Designed for an owner who reads diffs but
does not type git.

---

## Access URLs

| Thing | URL | Notes |
|---|---|---|
| **OpenWebUI (chat)** | `http://openwebui.lab.local:8080` | From the Windows browser. Needs the hosts entry below. First account you create = **admin**. Pick model **qwen3.5**. |
| Portainer | `https://localhost:9443` | From Windows browser. Self-signed cert → click "Advanced → Proceed". |
| Portainer (from Mac) | `https://<windows-LAN-ip>:9443` | Get the Windows IP via `ipconfig` in PowerShell. |
| LiteLLM (API only) | `http://litellm.lab.local:8080/v1` | OpenAI-compatible gateway. Needs a Bearer key; not a browser page. |
| k3d cluster API | `https://0.0.0.0:39093` | Only from inside WSL. `kubectl` already knows this. |

If you forgot the Portainer admin password, the only recovery is to
restart the container with the password-reset flag (ask Claude to do
this — one command).

### Reaching `*.lab.local` services in a browser

The `openwebui.lab.local` / `litellm.lab.local` names route through the
cluster's ingress (Traefik). Two things make them work:

1. **A hosts entry** on whichever machine's browser you're using. On the
   Windows box, edit `C:\Windows\System32\drivers\etc\hosts` — open
   Notepad via **right-click → Run as administrator** first, or it won't
   save — and add:
   ```
   127.0.0.1 openwebui.lab.local
   127.0.0.1 litellm.lab.local
   ```
2. **The port is `:8080`.** k3d maps the WSL2 host's port 8080 to the
   cluster ingress (and 8443 → HTTPS, once cert-manager lands in Phase
   5). So the URL is always `http://<name>.lab.local:8080`.

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
`host.docker.internal` anymore. Fix: ask Claude to recreate the
cluster — it's a 30-second cost.

---

## Slack approval message — how to read a diff (Phase 6+)

When Claude proposes a change, you'll get a Slack message like:

> 🤖 **Proposed:** Scale `openwebui` from 2 → 4 replicas.
> Reason: 95th-percentile latency exceeded 800ms for 5 min.
> [View diff](https://github.com/...) — [✅ Approve] [❌ Reject]

**The 30-second decision protocol:**
1. Click **View diff**. Look at the file path on the left.
2. Look at the highlighted lines — `+` is added, `-` is removed.
3. Ask yourself: **does the change match the English summary at the top?**
   - If yes → ✅
   - If the diff shows *more* changes than the summary mentions → ❌
   - If you don't recognize the file path → ❌ (then ask Claude)
4. If unsure, click ❌. ✅ is irreversible-ish (ArgoCD will apply it
   within seconds). ❌ is free — Claude can resubmit.

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

Phase 5.5 will give you a real kill switch. In the meantime, two
nuclear options:

**Stop the control-plane Claude pod (Phase 6+):**
```
kubectl scale deployment/claude-control -n platform-control --replicas=0
```

**Stop the whole cluster:**
```
k3d cluster stop devlab
```

Cluster is paused; nothing in it can do anything until `k3d cluster
start devlab`. State on the persistent volumes is preserved.

---

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
| **Destroy and recreate** the cluster | Ask Claude — there's a host-alias IP to re-bind and Portainer to re-attach. |
| List clusters k3d knows about | `k3d cluster list` |
| Get an admin kubeconfig file | `k3d kubeconfig get devlab` |

**Never run `k3d cluster delete devlab` casually.** It nukes
everything in the cluster. Persistent data (anything not on a
PersistentVolume backed by host paths) is gone. Phase 4 (GitOps)
makes recreate-from-scratch fast, but pre-Phase-4 you'd lose
state.

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
