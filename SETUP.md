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
| `catalog/postgres/secrets.enc.yaml` | `auth: {postgresPassword: <generate>}` + per-app roles under `auth.extraUsers` (complete entries — helm list overrides replace wholesale) |
| `catalog/monitoring/secrets.enc.yaml` | `kube-prometheus-stack: {grafana: {adminPassword: <generate>}}` + `oidc: {clientSecret: <generate>}` (Grafana's OIDC client secret — same value as the authentik file's `grafana_oidc_client_secret`) |
| `catalog/ai-gateway/secrets.enc.yaml` | `consumers: [{name: openwebui, apiKey: sk-owui-<generate>}]` |
| `catalog/openwebui/secrets.enc.yaml` | `gateway: {apiKey: <the SAME sk-owui value>}` + `sso: {clientSecret: <generate>}` (same value as the authentik file's `openwebui_oidc_client_secret`) |
| `catalog/authentik/secrets.enc.yaml` | `authentik.authentik.*`: `secret_key`, `bootstrap_password`, `bootstrap_token`, `postgresql.password` (must match the postgres `extraUsers` entry), and one `<app>_oidc_client_secret` per SSO client (openwebui, grafana, argocd) — each shared verbatim with that app's own SOPS file |
| `catalog/argocd/secrets.enc.yaml` | the deploy key from step 1.3 (structure shown there) + `argo-cd.configs.secret.extra."oidc.authentik.clientSecret"` (same value as the authentik file's `argocd_oidc_client_secret`) |

Tip: to add or rotate a single key without plaintext ever touching
disk, use `sops set <file> '["path"]["to"]["key"]' '"<value>"'` — it
edits the encrypted file in place and honors the stored encryption
rules.

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

The operator's read-only kubeconfig is minted automatically by
`bootstrap.sh` (step 9) into `~/.config/homelab-operator/kubeconfig` —
and re-minted on every run, because k3d assigns the API server a
random host port per cluster create. Nothing manual to do.

### 1.6 Sentinel — the capability broker (installs OUTSIDE the cluster)

Sentinel holds the kill switch, so it deliberately does not live in the
cluster it polices (`docs/adr/ADR-004-*`). It installs as two systemd
services on the host. Prerequisite on Ubuntu: `sudo apt install
python3.12-venv`.

```bash
cd sentinel
./scripts/mint-certs.sh                     # Sentinel's own CA, mTLS + console certs
sudo ./scripts/install-systemd.sh           # deploy to /opt, enable both units
sudo ./scripts/enroll-operator.sh           # prints a single-use enrollment code
```

**Trust Sentinel's CA once**, or the console is not a valid https origin
and your password manager will refuse to create a passkey on it. Import
`/etc/sentinel/certs/ca.crt` as a trusted root. Note that **Firefox keeps
its own certificate store** and ignores the Windows one: Settings →
Privacy & Security → Certificates → View Certificates → Authorities →
Import.

Then open **`https://localhost:8400/`** — https, and `localhost` rather
than `127.0.0.1`, because WebAuthn's Relying Party ID must be a domain —
paste the code, and register with your password manager, Windows Hello,
Touch ID, or a security key.

> **Why https on loopback?** Not for eavesdroppers; there are none.
> Browser passkey providers (1Password, Dashlane, …) decline to engage on
> a plain-http origin and silently fall back to the platform
> authenticator, which then fails with an unhelpful "operation failed for
> an unknown transient reason". `http://localhost` being a secure context
> by spec is true and not sufficient — what matters is what the extension
> will touch. Serving https is also what cloud needs anyway.

**Do that twice, with two different devices.** A second passkey is the
entire recovery plan: there is no account-recovery backdoor, because a
backdoor is a second front door to the kill switch.

Re-run `sudo ./scripts/install-systemd.sh` after a `git pull` — that is
the deploy step, and it is deliberate rather than a git checkout being
hot-swapped underneath a running service. The same script is what
cloud-init runs on a droplet; nothing in it is specific to this
machine except the broker address, which it detects and writes to
`/etc/sentinel/sentinel.env`.

### 1.7 After a reboot: the platform is manually started

**Known and accepted** (owner decision, 2026-07-27). Windows does not
start WSL2 on its own, and everything else waits on it: no WSL means no
Docker, which means no cluster and no Sentinel. Every container carries
a restart policy and both Sentinel units are `Restart=always`, so the
whole platform comes back **once WSL is running** — but something has to
run it.

So after a Windows reboot, open a WSL terminal (or RDP in and open one).
That is the start command. Check it came back with:

```bash
docker ps                                   # k3d nodes + portainer
systemctl status sentinel-broker sentinel-admin
kubectl get applications -n argocd
```

If you later want it unattended, a Windows Task Scheduler entry at boot
running `wsl.exe -d Ubuntu -e true` is enough to trigger the chain. It
is deliberately not done today: an always-on lab is a different
commitment, and `Restart=always` should not be read as a promise the
platform cannot keep.

---

## Part 2 — every web interface, and where its password lives

| Interface | What it does | How to reach it | Login |
|---|---|---|---|
| **OpenWebUI** | The chat UI — what end users see | `https://openwebui.lab.local:8443` (add `127.0.0.1 openwebui.lab.local` to your hosts file; http on :8080 redirects here). Browser warns until you trust the lab CA — export + import per the cheatsheet's "TLS: the lab CA". | **Your Authentik account** — "Continue with Authentik" or the portal tile (needs group `openwebui-users` or `openwebui-admins`; admins group = app admin). Break-glass: the pre-SSO local signup account (first local signup was admin). |
| **ArgoCD** | GitOps engine — the platform as it compares to git; every deploy, diff, and sync | `https://argocd.lab.local:8443` (hosts entry `127.0.0.1 argocd.lab.local`; port-forward fallback: `kubectl port-forward -n argocd svc/argocd-server 8081:80`) | **Your Authentik account** — "LOG IN VIA AUTHENTIK" or the portal tile (`argocd-admins` = full admin, `argocd-users` = read-only). Break-glass: `admin` / `kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' \| base64 -d` |
| **Grafana** | Dashboards — cluster health, and the AI token-rate/autoscaling view | `https://grafana.lab.local:8443` (hosts entry `127.0.0.1 grafana.lab.local`; port-forward fallback: `kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80`) | **Your Authentik account** — "Sign in with Authentik" or the portal tile (`grafana-admins` = org Admin, `grafana-users` = Viewer). Break-glass: `admin` / the value in `catalog/monitoring/secrets.enc.yaml` (`sops -d` to read) |
| **Prometheus** | Raw metrics + PromQL console (KEDA's data source) | `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090` → `http://localhost:9090` | none (unauthenticated, cluster-internal by design) |
| **Alertmanager** | Alert routing (wired to chat channels in Phase 7) | `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093` → `http://localhost:9093` | none |
| **Authentik** | Identity provider — SSO admin console (Phase 5B) | `https://authentik.lab.local:8443` (hosts entry `127.0.0.1 authentik.lab.local`) | `akadmin` / the `bootstrap_password` you set in `catalog/authentik/secrets.enc.yaml` (applied on FIRST boot only — changing it later in the file does nothing; change it in the UI instead) |
| **Portainer** | Docker/K8s visual management (optional) | `https://localhost:9443` | **Admin account is created on your FIRST visit** — go there within ~10 min of `bootstrap.sh` or Portainer locks itself and needs a container restart to re-open enrollment. |
| **AI Gateway** | OpenAI-compatible LLM API (not a web page) | `http://ai-gateway/v1` — **in-cluster only**, on purpose | per-consumer Bearer keys from `catalog/ai-gateway/secrets.enc.yaml` |
| **Postgres** | Relational store (no UI) | in-cluster `postgres:5432` | `postgres` / your value from `catalog/postgres/secrets.enc.yaml`; shell: `kubectl exec -it -n chat postgres-0 -- psql -U postgres` |

Rule of thumb: **nothing in this platform has a vendor default
password.** Every credential is either generated by you into a SOPS
file (Grafana, Postgres, gateway keys), generated by the tool at
install (ArgoCD), or created interactively on first visit (OpenWebUI,
Portainer). If you ever find yourself typing `admin/admin`, something
is wrong.

**SSO is live (Phase 5):** OpenWebUI, Grafana, and ArgoCD all
authenticate through Authentik — one account, roles per app via
Authentik groups, and each app's portal tile is a one-click login
(the tiles target the apps' OIDC initiation routes). Onboarding a
teammate = create one Authentik user (Directory → Users), set a
password, add them to the `*-users`/`*-admins` groups for the apps
they need. The SSO config itself is config-as-code — Authentik
blueprints in `catalog/authentik/templates/oidc-blueprints.yaml` —
so it assembles headless on a fresh deploy; only group *membership*
is runtime data. Every app keeps its local break-glass login (table
above) for the day the IdP itself is down.

### Trusting the lab CA (one-time, per client machine)

The platform signs its own `*.lab.local` HTTPS certificates with a
private CA (see `catalog/cert-manager/`). Until a machine trusts that
CA, every lab URL shows a certificate warning. The import is
additive, takes two minutes, and covers every current *and future*
lab service. **Local-only artifact:** on a public cluster with a real
domain, the issuer swaps to Let's Encrypt and this section does not
apply at all.

1. **Export the CA's public cert** (private key never leaves the
   cluster):
   ```bash
   kubectl get secret lab-local-ca -n cert-manager \
     -o jsonpath='{.data.ca\.crt}' | base64 -d > lab-local-ca.crt
   ```
2. **Verify you exported what you think** — compare against your
   cluster's fingerprint before trusting anything:
   ```bash
   openssl x509 -in lab-local-ca.crt -noout -subject -fingerprint -sha256
   ```
3. **Import it:**
   - **Windows** (elevated PowerShell — Start → "powershell" →
     right-click → Run as administrator):
     ```
     certutil -addstore Root C:\path\to\lab-local-ca.crt
     ```
     Undo later with: `certutil -delstore Root "homelab lab.local CA"`
   - **macOS**:
     ```bash
     sudo security add-trusted-cert -d -r trustRoot \
       -k /Library/Keychains/System.keychain lab-local-ca.crt
     ```
     Undo later with: `sudo security delete-certificate -c "homelab lab.local CA" /Library/Keychains/System.keychain`
4. **Restart the browser** before checking — browsers cache TLS
   verdicts, and a stale warning after a successful import is the #1
   false alarm here.
5. Visit `https://openwebui.lab.local:8443` — padlock should be clean.

Lifecycle: leaf certificates renew themselves (~90 days, automatic,
invisible). The CA lasts 10 years; when it renews, clients re-import
once — that's a planned migration, not maintenance (details:
operator cheatsheet → "TLS: the lab CA").
