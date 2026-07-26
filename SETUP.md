# Setup — the parts that live outside the cluster

The [README](README.md) shows what this platform is; this file is the
exact walkthrough of everything you must do **outside** the repo to run
your own copy — GitHub configuration, secret regeneration, and a map of
every web interface the platform deploys (with usernames and where each
password actually comes from).

> UI instructions are accurate for **GitHub as of July 2026**. GitHub
> moves things; the *intent* of each step is stated so you can survive
> a redesign.

---

## Part 1 — one-time setup, in order

### 1.0 Fork and clone

Fork this repo to your account, then clone your fork into WSL2/Linux
(keep it on the Linux filesystem, not `/mnt/c/...`). Everything below
happens against **your fork**.

Prerequisites (bootstrap.sh checks these): `docker`, `k3d`, `kubectl`,
`helm` + the [helm-secrets](https://github.com/jkroepke/helm-secrets)
plugin, [`sops`](https://github.com/getsops/sops),
[`age`](https://github.com/FiloSottile/age).

### 1.1 Your encryption key (age) — and why you must regenerate secrets

```bash
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt   # prints your PUBLIC key (age1...)
```

Edit `.sops.yaml` at the repo root: replace the `age:` recipient with
**your** public key.

**Important:** the `secrets.enc.yaml` files in this repo are encrypted
to the *original author's* key — you cannot decrypt them, and you don't
need to. You regenerate each one with your own values:

| File | What goes in it |
|---|---|
| `catalog/postgres/secrets.enc.yaml` | `auth: {postgresPassword: <generate>}` |
| `catalog/monitoring/secrets.enc.yaml` | `kube-prometheus-stack: {grafana: {adminPassword: <generate>}}` |
| `catalog/ai-gateway/secrets.enc.yaml` | `consumers: [{name: openwebui, apiKey: sk-owui-<generate>}]` |
| `catalog/openwebui/secrets.enc.yaml` | `gateway: {apiKey: <the SAME sk-owui value>}` — this is OpenWebUI's copy of its consumer key |
| `catalog/argocd/secrets.enc.yaml` | the deploy key from step 1.3 (structure shown there) |

The pattern for each (write plaintext → encrypt in place — the
`.sops.yaml` rule only matches files at their catalog path):

```bash
cat > catalog/postgres/secrets.enc.yaml <<EOF
auth:
  postgresPassword: $(openssl rand -hex 16)
EOF
sops -e -i catalog/postgres/secrets.enc.yaml
```

Verify every file shows `ENC[...]` values before committing. Key names
matter: only keys matching `.sops.yaml`'s `encrypted_regex` (`apiKey`,
`*Password`, `*Token`, `*Secret`, `data`, `stringData`) get encrypted.

### 1.2 Repo visibility and the merge gate (ruleset)

Branch rules are **not enforced on private personal repos on the free
plan** — either make your fork public, or buy GitHub Pro (~$4/mo).

Create the ruleset — direct URL:
`https://github.com/<you>/<repo>/settings/rules/new?target=branch`

1. Name: `protect-main`
2. **Enforcement status → Active** ← it defaults to *Disabled*, which
   silently does nothing. Everyone misses this.
3. **Bypass list → Add bypass → Repository admin** — so YOUR direct
   pushes still work during build sessions (each bypass is logged by
   GitHub). Remove this bypass when you stop hand-building — that's
   the one-click hardening.
4. Target branches → Add target → **Include default branch**.
5. Rules: tick **Require a pull request before merging** → Required
   approvals: **1**. Leave *Restrict deletions* and *Block force
   pushes* ticked.
6. Create.

### 1.3 ArgoCD's read-only deploy key

```bash
ssh-keygen -t ed25519 -N "" -C "argocd-readonly" -f /tmp/argocd-deploy-key
```

GitHub: your repo → **Settings → Deploy keys → Add deploy key** —
title `argocd-readonly`, paste the **.pub** file's contents, and leave
**"Allow write access" UNCHECKED** (that unchecked box is the security
property: write access to git does not exist inside the cluster).

Private half goes into `catalog/argocd/secrets.enc.yaml`:

```bash
python3 - <<'EOF'
import yaml
doc = {'argo-cd': {'configs': {'repositories': {'homelab': {
    'url': 'git@github.com:<you>/<repo>.git',
    'name': 'homelab', 'type': 'git',
    'sshPrivateKey': open('/tmp/argocd-deploy-key').read(),
}}}}}
open('catalog/argocd/secrets.enc.yaml','w').write(yaml.safe_dump(doc, sort_keys=False))
EOF
sops -e -i catalog/argocd/secrets.enc.yaml
rm /tmp/argocd-deploy-key /tmp/argocd-deploy-key.pub
```

Sanity check the registration: `ssh -i <key> -T git@github.com` must
greet you with the **repo name** (`Hi you/repo!`) — an account-name
greeting means the key landed in your account's SSH keys (account-wide
power, wrong place; remove it and redo).

### 1.4 The operator's identity (GitHub App) — needed only for the PR-bot

Direct URL: `https://github.com/settings/apps/new`

1. Name: `<something>-homelab-operator` (globally unique).
2. Homepage URL: your repo URL.
3. **Webhook → UNCHECK "Active"** ← or the form demands a webhook URL
   and refuses to submit. The other classic miss.
4. Repository permissions: **Contents: Read and write** + **Pull
   requests: Read and write** (Metadata flips to read-only itself).
   Why write, if it "only does PRs"? A PR is a request to merge a
   branch — the bot must push that proposal branch, which IS the
   write. The ruleset from 1.2 is what walls it off `main`.
5. "Only on this account" → **Create GitHub App**.
6. Note the **App ID** (number at the top), click **Generate a private
   key** (a `.pem` downloads), then left sidebar **Install App** →
   your account → **Only select repositories** → your fork.

Wire it up on the WSL host (never in the repo):

```bash
mkdir -p ~/.config/homelab-operator
mv ~/Downloads/<the-downloaded>.pem ~/.config/homelab-operator/github-app.pem   # adjust source path
chmod 600 ~/.config/homelab-operator/github-app.pem
cat > ~/.config/homelab-operator/env <<EOF
GH_APP_ID=<your App ID>
GH_APP_KEY_FILE=$HOME/.config/homelab-operator/github-app.pem
GH_REPO=<you>/<repo>
EOF
chmod 600 ~/.config/homelab-operator/env
```

Tokens are minted on demand (1-hour TTL) by
`ops/operator/bin/gh-app-token.sh`. Kill switch: uninstall the App
(repo Settings → GitHub Apps).

#### First flight: prove the gate

When the operator opens its first PR, don't merge it yet — spend two
minutes trying to defeat your own setup while it's cheap. From the repo
root, become the App and attempt the two things the gate exists to
prevent (one attempt each; **both must fail**):

```bash
source ~/.config/homelab-operator/env
export GH_TOKEN=$(ops/operator/bin/gh-app-token.sh)   # you are now the App
PR=<the PR number>

# 1) the App approves its own PR — expect HTTP 422:
#    "Can not approve your own pull request"
gh api -X POST "repos/$GH_REPO/pulls/$PR/reviews" -f event=APPROVE

# 2) the App merges with zero reviews — expect HTTP 405:
#    "Repository rule violations found"
gh api -X PUT "repos/$GH_REPO/pulls/$PR/merge" -f merge_method=merge

unset GH_TOKEN                                        # back to being you
```

Read the two failures differently. The 422 is GitHub's unconditional
author≠approver rule — nothing for you to maintain. The 405 is **your
ruleset from 1.2 working**, and it is the only one of the two that is
configuration: if you ever see a 200 there, the ruleset is inactive,
mis-targeted, or the App has landed on a bypass list — stop and fix
that before merging anything. Re-run this pair after any ruleset
change. (Why the test is safe: worst case, a PR you were about to
merge anyway merges — and you've learned your gate was open.)

### 1.5 Light it up

```bash
./bootstrap.sh                              # cluster + ArgoCD; the catalog self-assembles
kubectl get applications -n argocd -w       # watch it converge
bash ops/operator/launch.sh                 # (optional) start the PR-only operator
```

The operator additionally needs the read-only kubeconfig once:
`kubectl apply -f k3d/operator-view-rbac.yaml`, then export the
`operator-view-token` Secret into
`~/.config/homelab-operator/kubeconfig` (see `k3d/operator-view-rbac.yaml`
comments; bootstrap integration is on the roadmap).

---

## Part 2 — every web interface, and where its password lives

| Interface | What it does | How to reach it | Login |
|---|---|---|---|
| **OpenWebUI** | The chat UI — what end users see | `http://openwebui.lab.local:8080` (add `127.0.0.1 openwebui.lab.local` to your hosts file) | **First account you sign up becomes admin.** No default password exists — the signup is the setup. |
| **ArgoCD** | GitOps engine — the platform as it compares to git; every deploy, diff, and sync | `kubectl port-forward -n argocd svc/argocd-server 8081:80` → `http://localhost:8081` | `admin` / `kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' \| base64 -d` |
| **Grafana** | Dashboards — cluster health, and the AI token-rate/autoscaling view | `kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80` → `http://localhost:3000` | `admin` / the value YOU put in `catalog/monitoring/secrets.enc.yaml` — read it back with `sops -d catalog/monitoring/secrets.enc.yaml` |
| **Prometheus** | Raw metrics + PromQL console (KEDA's data source) | `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090` → `http://localhost:9090` | none (unauthenticated, cluster-internal by design) |
| **Alertmanager** | Alert routing (wired to chat channels in Phase 7) | `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093` → `http://localhost:9093` | none |
| **Portainer** | Docker/K8s visual management (optional) | `https://localhost:9443` | **Admin account is created on your FIRST visit** — go there within ~10 min of `bootstrap.sh` or Portainer locks itself and needs a container restart to re-open enrollment. |
| **AI Gateway** | OpenAI-compatible LLM API (not a web page) | `http://ai-gateway/v1` — **in-cluster only**, on purpose | per-consumer Bearer keys from `catalog/ai-gateway/secrets.enc.yaml` |
| **Postgres** | Relational store (no UI) | in-cluster `postgres:5432` | `postgres` / your value from `catalog/postgres/secrets.enc.yaml`; shell: `kubectl exec -it -n chat postgres-0 -- psql -U postgres` |

Rule of thumb: **nothing in this platform has a vendor default
password.** Every credential is either generated by you into a SOPS
file (Grafana, Postgres, gateway keys), generated by the tool at
install (ArgoCD), or created interactively on first visit (OpenWebUI,
Portainer). If you ever find yourself typing `admin/admin`, something
is wrong.

SSO note: OpenWebUI, Grafana, and ArgoCD all move behind Authentik
single sign-on in Phase 5 — this table is the pre-SSO reality.
