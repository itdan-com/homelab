# Phase 1 — Host setup, Docker layer, git literacy primer

**Goal:** WSL2 + docker-ce + kubectl/helm/k3d installed, Portainer running, `devlab` k3d cluster up with host-gateway mapping wired in, and the owner has minimum read-only git literacy.

**Status:** ✅ COMPLETE (2026-05-17). All items done except 1.7 (git
primer), deliberately deferred into `docs/operator-cheatsheet.md` —
its unchecked boxes are kept for reference only. (Status line was
stale until 2026-07-25 doc-hygiene pass.)

**Why this phase exists:** Every phase that follows depends on docker-ce + a working cluster. Portainer gives a Docker-level GUI (replacing what Docker Desktop's GUI used to show). The git primer is small but essential — the owner will read diffs in every Slack approval message from Phase 6 onward.

---

## Checklist

### 1.1 Enable systemd in WSL2

- [x] Edit `/etc/wsl.conf` to include `[boot]` with `systemd=true`.
- [x] From a PowerShell window on the Windows host: `wsl --shutdown`.
- [x] Reopen WSL; verify with `systemctl is-system-running` (should be `running` or `degraded`, not `offline`).
- [x] **Success criterion:** `systemctl status` runs without "System has not been booted with systemd" error.

### 1.2 Install docker-ce

- [x] Add Docker's official GPG key and apt repository (follow current Ubuntu install instructions from docs.docker.com — versions drift, check the live page).
- [x] `sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`
- [x] `sudo systemctl enable --now docker`
- [x] `sudo usermod -aG docker $USER`; then `newgrp docker` or restart shell so the group takes effect without a logout/login dance.
- [x] **Success criterion:** `docker run --rm hello-world` succeeds without sudo.

### 1.3 Install kubectl, helm, k3d

- [x] kubectl: from the official Kubernetes apt repo (pkgs.k8s.io).
- [x] helm: `curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash`
- [x] k3d: `curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash`
- [x] **Success criterion:** `kubectl version --client`, `helm version`, and `k3d version` each print a version cleanly.

### 1.4 Deploy Portainer CE

- [x] `docker volume create portainer_data`
- [x] `docker run -d -p 9000:9000 -p 9443:9443 --name portainer --restart=always -v /var/run/docker.sock:/var/run/docker.sock -v portainer_data:/data portainer/portainer-ce:latest`
- [x] From a browser on the Windows box or Mac: open `https://<wsl-ip-or-windows-host-ip>:9443`; complete initial admin user setup within 5 min (the bootstrap window times out).
- [x] **Success criterion:** Portainer dashboard shows the local Docker environment with the `hello-world` container in its history.

### 1.5 Create `devlab` k3d cluster with host-gateway mapping

- [x] Create cluster: `k3d cluster create devlab --servers 1 --agents 3` plus the appropriate flag to wire `host.docker.internal` to `host-gateway` (consult `k3d cluster create --help` for current syntax — has changed across k3d versions).
- [x] `kubectl get nodes` — expect 1 server + 3 agent nodes Ready.
- [x] Test in-pod DNS: `kubectl run --rm -it dnstest --image=busybox -- nslookup host.docker.internal` should resolve.
- [x] **Success criterion:** an in-cluster pod can reach `host.docker.internal:11434` (test with `curl` if Ollama is running on the Windows host, or `nc -zv` to any host port).

### 1.6 Register k3d cluster in Portainer

- [x] In Portainer: Environments → Add environment → Kubernetes → import via kubeconfig from `~/.kube/config` (or use the kubeconfig k3d writes when the cluster is created).
- [x] Verify the cluster appears with nodes and namespaces visible.
- [x] **Success criterion:** Portainer shows the `kube-system` namespace's pods.

### 1.7 Git literacy primer (read-only)

**DEFERRED — folded into `docs/operator-cheatsheet.md` under the "Slack approval message" section.** The practical literacy (read a diff, decide ✅/❌) is captured there as a 30-second decision protocol with examples, ready to consult when the first real Phase 6 approval lands.

Original checklist (kept for reference / can be revisited as a teaching session anytime):

- [ ] What is a **commit**? (snapshot of files at a point in time, tied to an author, message, and hash)
- [ ] What is a **diff**? (the change from one commit to the next; `+` lines added, `-` lines removed)
- [ ] What is a **branch**? (a moveable pointer to a commit; lets multiple histories coexist)
- [ ] What is a **PR / pull request**? (a proposal to merge one branch into another, with review)
- [ ] How to read a diff in a GitHub PR view (file list left, line-by-line right, expand context with the arrows).
- [ ] **Success criterion:** given a fake PR diff with a 3-line change, the owner can describe what changed and confidently decide ✅/❌. This is the literacy needed to operate the Phase 6 approval flow.

---

## Phase exit criteria

- All checklist items above checked.
- `kubectl get nodes` shows a healthy 4-node `devlab` cluster.
- Portainer dashboard accessible from Mac browser over LAN.
- Owner can read a diff and approve/reject confidently.
- `STATUS.md` updated: Active phase → Phase 2, Recent activity log appended.

## Notes captured during execution

_Append observations, surprises, and things-to-revisit-later here as the phase progresses. Items that don't belong to Phase 1 get promoted to `STATUS.md` Backlog._

- 2026-05-16 — 1.1 was already done before this session: `/etc/wsl.conf` already had `[boot] systemd=true`, `systemctl is-system-running` reported `running`, PID 1 was `systemd`. No edit or `wsl --shutdown` needed.
- 2026-05-17 — 1.2 done. docker-ce 29.5.0, containerd.io 2.2.3 installed from Docker's official noble repo. Narrow NOPASSWD sudoers drop-in at `/etc/sudoers.d/homelab-setup-sudoers` (deletable post-Phase-1) allows Claude to drive apt/systemctl/usermod without password prompts; shells (bash/sh/su) intentionally excluded. Bob's existing shells need re-login or `newgrp docker` to see docker group; new shells (incl. Claude's Bash tool) already see it.
- 2026-05-17 — 1.3 done. kubectl v1.32.13 (apt-managed, pkgs.k8s.io v1.32 channel), helm v3.21.0, k3d v5.8.3 (ships k3s v1.31.5 by default). kubectl↔k3s skew is 1 minor — within supported window.
- 2026-05-17 — 1.4 done. Portainer CE v2.39.2 in `portainer` container, named volume `portainer_data`, restart=always. Bootstrap window timed out the first time; restart resets it. Bob created admin user.
- 2026-05-17 — 1.5 done. devlab cluster: 1 server + 3 agents, all Ready, k3s v1.31.5. `host.docker.internal` aliased to current WSL default-route gateway `172.19.80.1` (recorded in `k3d/devlab.host-alias-ip`). TCP probe from in-pod to Windows host port 3389 succeeded. **Gotcha discovered:** busybox `nslookup` returns the wrong answer for `host.docker.internal` (resolved to 192.168.50.102) — DO NOT use busybox for cluster-DNS troubleshooting. `nicolaka/netshoot` with `dig @<coredns-svc-ip>` gives the correct answer. **Gap flagged for Phase 5.5:** k3s default CNI is Flannel which does NOT enforce NetworkPolicy. Sentinel's "MCP servers refuse traffic that did not come through the proxy" requires policy enforcement; before Phase 5.5 we must switch to Cilium or Calico. Added to STATUS.md backlog.
- 2026-05-17 — 1.6 done. Portainer CE 2.39 does NOT support the kubeconfig-import flow (BE-only). Used the canonical CE path: deployed Portainer Agent into the cluster via `https://downloads.portainer.io/ce-lts/portainer-agent-k8s-nodeport.yaml` (creates ns `portainer`, ClusterRoleBinding `portainer-crb-clusteradmin`, NodePort svc on 30778). Portainer container was attached to the `k3d-devlab` Docker network so it can route to the node IPs directly. **Gotcha during validation:** k3d node containers report the *whole* WSL2 VM resources (31 GiB / 16 CPUs each) because there are no cgroup limits, so Portainer's per-environment view shows 4× overcounted memory/CPU. Real risk is k8s scheduler over-commitment when we deploy real workloads — flagged in STATUS.md backlog, fix during Phase 5.5 CNI-swap cluster rebuild by adding per-node memory limits in a k3d cluster config YAML. Also discovered `.wslconfig` doesn't exist (CLAUDE.md vs reality mismatch) — flagged for owner decision.
