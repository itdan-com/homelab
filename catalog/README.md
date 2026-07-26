# Catalog

This directory holds the **service catalog**: one Helm chart per workload
that runs on the platform. Adding a new service to the platform is
designed to be a single act — drop a chart in here, set a few labels,
commit. Downstream automation (SSO, observability, security gating,
control-plane Claude awareness) keys off the labels and lights up on its
own.

**This README is the contract every chart must follow and every later
phase keys off.** If you change the contract here, you change the
behavior of the whole platform. Read carefully before editing.

---

## Why a labeled catalog

Most internal platforms grow as a series of bespoke integrations:
"OpenWebUI needs an Authentik client, so someone manually edits the
Authentik config; the AI gateway needs to be in the LLM cost dashboard,
so someone manually adds it to Grafana; etc." Every new service is a
multi-place patch.

The catalog inverts that. Every chart **self-declares** what it needs
via labels in its `Chart.yaml`:

```yaml
annotations:
  catalog.homelab/needs-sso: "true"
  catalog.homelab/llm-traffic: "true"
  catalog.homelab/tier: "dev"
```

Then in later phases:

- **Phase 4 (GitOps / app-of-apps):** ArgoCD discovers the chart
  automatically because it sits in `catalog/`.
- **Phase 5 (team enablement):** Authentik's onboarding controller sees
  `needs-sso: true` and creates an OIDC client for it.
- **Phase 5.5 (Sentinel):** The Sentinel proxy injects itself in front
  of any chart with `exposes-mcp: true`.
- **Phase 6 (control-plane Claude):** Claude reads the label set to
  decide what an action affects (e.g. "scaling this means more LLM
  traffic — check the ai-gateway autoscaling too").
- **Phase 7 (observability):** Charts with `llm-traffic: true`
  auto-register with the LLM cost dashboard.

The labels are the API. The chart is the implementation.

---

## Directory layout

Flat, one directory per service:

```
catalog/
├── README.md             # this file — the contract
├── _template/            # canonical skeleton chart; copy this when adding a service
├── postgres/             # Phase 2: dependency for the others
├── ai-gateway/           # Phase 2.5: LLM gateway (Envoy AI Gateway)
├── openwebui/            # Phase 2: chat UI
├── authentik/            # Phase 5: SSO
├── langfuse/             # Phase 5: LLM observability + prompt gallery
├── minio/                # Phase 5: S3-compatible storage
├── cert-manager/         # Phase 5: TLS for *.lab.local
└── ...                   # add a chart, label it, that's onboarding
```

No grouping by team or domain. The flat layout breaks down somewhere
past ~20 services; this platform won't get there. Flat keeps the "drop
in a chart" workflow trivial.

---

## Adding a new service (the 30-second workflow)

This is the workflow the whole pattern is designed around:

```bash
cp -r catalog/_template catalog/my-service
cd catalog/my-service
# edit Chart.yaml: name, description, annotations (the labels)
# edit values.yaml: image, ports, resource requests
# edit templates/: add Deployment, Service, etc.
helm install my-service . -n my-namespace --create-namespace
git add catalog/my-service && git commit -m "catalog: add my-service"
```

Phase 4 onward: just the `git commit`. ArgoCD does the install.

---

## The label schema (v1)

Six labels, namespaced under `catalog.homelab/` to avoid colliding with
Kubernetes' own. The namespace prefix is important — bare labels like
`tier` are reserved by various controllers and would silently conflict.

Labels split into two groups based on where they live:

- **Chart-level (set in `Chart.yaml.annotations`, fixed per chart):**
  properties of *what the chart is*. A chart either talks to an LLM or
  it doesn't.
- **Release-level (set in `values.yaml`, overridable per deployment):**
  properties of *this particular deployment*. The same chart can be
  sandbox in one namespace and prod in another. This is what enables
  the GitOps promotion flow (deploy to `chat-sandbox` → test → promote
  to `chat-dev` → test → promote to `chat-prod`).

| Label | Scope | Type | Values | What it does |
|---|---|---|---|---|
| `needs-sso` | chart-level | bool | `"true"` / `"false"` | Authentik (Phase 5) creates an OIDC client and injects the ingress middleware. |
| `llm-traffic` | chart-level | bool | `"true"` / `"false"` | The chart carries LLM traffic (is, or talks through, the AI gateway). Phase 7 auto-adds it to the LLM cost dashboard. Phase 5.5 may insert Sentinel mediation in front of cloud-LLM traffic. |
| `wants-vector` | chart-level | bool | `"true"` / `"false"` | The chart needs the cluster vector DB (deployed later). Phase 4+ wires the connection automatically. |
| `exposes-mcp` | chart-level | bool | `"true"` / `"false"` | The chart runs a Model Context Protocol server. Phase 5.5 inserts the Sentinel proxy in front; Phase 6 control-plane Claude auto-discovers it. **Setting this label commits the chart to Sentinel gating — don't set it on workloads that aren't MCP servers.** |
| `tier` | release-level | enum | `sandbox` / `dev` / `prod` | Sentinel (Phase 5.5+) applies different trust gradients per tier. `sandbox`: auto-approves common capabilities, no human tap for routine actions, kill-switch off. `dev`: human tap required but cost caps relaxed, verbose logging on. `prod`: every external action prompts the human; kill-switch primed; may be disconnected from external SaaS by default. Promotion is "deploy the same chart to the next-tier namespace via a git commit." |
| `data-class` | release-level | enum | `none` / `internal` / `secret` | Drives secrets policy: `secret`-class deployments MUST use SOPS-encrypted values (see below). `internal` is the default. `none` for deployments with no data at all. Often varies by tier — a `langfuse` deployment is `internal` in sandbox (fake data) but `secret` in prod (real PII). |

### Reserved for v2 (future labels we may add)

Mentioned now so the pattern is extensible without breaking v1:

- `external-deps` — list of MCP servers the chart needs (e.g.
  `[github, slack]`). Used by control-plane Claude to reason about
  blast radius of an action.
- `cost-sensitive` — bool. Phase 3 autoscaling and Phase 8 cloud
  guardrails respect this.
- `owner` — Slack handle or email. For the audit log + alerting in
  Phase 7.

If you find yourself wanting one of these mid-build, raise it; we'll
add the label and update the contract here in one commit.

---

## Label propagation (how labels reach the cluster)

The labels live in `Chart.yaml.annotations` as the **source of truth**
(human-readable, version-controlled, surfaced by `helm show chart`).
They are then propagated as **Kubernetes labels** onto every rendered
object (Deployment, Service, ConfigMap, Ingress, etc.) via a shared
helper in `templates/_helpers.tpl`. This is what makes `kubectl get
deploy -l catalog.homelab/llm-traffic=true` and ArgoCD selectors work.

Every chart includes this stanza in its templates' metadata:

```yaml
metadata:
  labels:
    {{- include "catalog.labels" . | nindent 4 }}
```

The `catalog.labels` helper reads `Chart.yaml.annotations` and emits
matching K8s labels. It lives in `_template/templates/_helpers.tpl` and
is copied verbatim into each real chart — it is **the only file the
contract requires to stay byte-identical in every chart** (see "Drift
discipline" under the `_template/` section). Do not hand-edit the
helper inside a real chart. If the helper needs to change, edit
`_template/templates/_helpers.tpl` and `cp` the updated version into
every chart in the same commit, `diff`-ing each copy to prove it.

---

## Secrets approach (Phase 2 → Phase 6 migration path)

**Phase 2 (current): SOPS + age.** Secrets live in git, encrypted at
rest with [age](https://github.com/FiloSottile/age) keys. The
[helm-secrets](https://github.com/jkroepke/helm-secrets) plugin decrypts
them at apply-time. This is the GitOps-canonical pattern (used by k3s,
GitLab, Mozilla) and migrates cleanly to ArgoCD in Phase 4 (which has
first-class SOPS support).

**Why not plain Kubernetes Secrets + `.gitignore`?** A `.gitignore`'d
`values-secrets.yaml` breaks the "fully declarative" property — cluster
rebuild loses secrets, and the audit trail (git history) is missing the
secret-rotation record. SOPS keeps everything in git, encrypted.

**Why not External Secrets Operator + Vault?** Overkill for a homelab,
and ESO would later compete with Sentinel for the credential-broker
role. Skip.

### Setup (one-time, run on the WSL2 host)

```bash
# Install age and SOPS
sudo apt install -y age
curl -L https://github.com/getsops/sops/releases/latest/download/sops-v3.9.0.linux.amd64 -o /tmp/sops
sudo install /tmp/sops /usr/local/bin/sops

# Generate the age key for this host
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
# Copy the PUBLIC key (starts with "age1...") — needed for .sops.yaml

# Install helm-secrets plugin
helm plugin install https://github.com/jkroepke/helm-secrets
```

The **private key** (`~/.config/sops/age/keys.txt`) stays on the WSL2
host, outside the repo. The **public key** goes into `.sops.yaml` at
the repo root, which tells SOPS which key to encrypt to:

```yaml
# .sops.yaml at repo root
creation_rules:
  - path_regex: catalog/.*/secrets\.enc\.yaml$
    encrypted_regex: "^(data|stringData|.*[Pp]assword|.*[Tt]oken|.*[Kk]ey)$"
    age: age1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Per-chart usage

A chart that needs secrets has a `secrets.enc.yaml` alongside its
regular `values.yaml`:

```yaml
# catalog/ai-gateway/secrets.enc.yaml (encrypted in git)
consumers:
    - name: openwebui                                  # key NAMES stay readable
      apiKey: ENC[AES256_GCM,data:...,tag:...,type:str] # VALUES encrypt (per .sops.yaml regex)
```

Apply with:

```bash
helm secrets upgrade --install ai-gateway . -n chat -f values.yaml -f secrets.enc.yaml
```

Edit with `sops catalog/ai-gateway/secrets.enc.yaml` — opens an editor
with the plaintext; re-encrypts on save. NOTE: only keys matching
`.sops.yaml`'s `encrypted_regex` (`apiKey`, `*password`, `*Token`,
`*Secret`, …) get encrypted — name your secret fields accordingly.

### Phase 6 migration plan

Once Sentinel is live, secrets graduate from "long-lived SOPS-encrypted
file in git" to "short-lived Sentinel-issued token requested at
flow-start." SOPS doesn't go away — it stays the right answer for
bootstrap secrets (the age key for the next layer, Sentinel's own
admin password, etc.). The split:

- **Bootstrap / infra secrets** (DB passwords, certificate roots, age
  keys for sub-systems) → stay in SOPS-encrypted files in git.
- **External-system credentials** (GitHub PAT, Slack token, OpenAI key,
  etc.) → move to Sentinel; charts request short-lived tokens at
  flow-start via the Sentinel proxy.

Charts with `data-class: secret` will need an audit pass at Phase 6
start to identify which of their secrets are bootstrap (stays in SOPS)
vs external (moves to Sentinel).

---

## Ingress and TLS

**Phase 2 (current): Traefik IngressRoute + `*.lab.local`.** k3d ships
Traefik as its default ingress controller. Every chart gets an
`IngressRoute` for `<service>.lab.local`. Add one line per service to
the Mac's `/etc/hosts`:

```
192.168.1.42  openwebui.lab.local portainer.lab.local
```

(Replace with the actual WSL2 host IP visible from the Mac. On Windows
side use `ipconfig` from a Mac-reachable network interface.)

**Why not NodePort?** NodePort means picking and remembering a port per
service, and breaks browser-bookmarkable URLs. Friction compounds with
catalog growth. Traefik IngressRoute is one extra YAML object per
chart and we get clean URLs from day one.

**Phase 5 adds TLS.** cert-manager + a local CA will issue certificates
for `*.lab.local` so the catalog moves from `http://` to `https://`
with no chart changes — just an annotation on each `IngressRoute`.

---

## The `_template/` chart

`catalog/_template/` is the canonical skeleton. To onboard a new
service:

```bash
cp -r catalog/_template catalog/my-service
```

Then edit `Chart.yaml`, `values.yaml`, and `templates/` to fit. The
critical pieces — `_helpers.tpl` (label propagation), `Chart.yaml`
annotation block (label schema), and the `.metadata.labels` stanza
on every resource — are pre-wired so you can't accidentally break
the contract by forgetting to propagate a label.

**Drift discipline (contract v2 — issue #5).** Helm has no cross-chart
include, so `_template` is a copy source, not a live dependency. The
contract distinguishes two kinds of file:

- **`_helpers.tpl` must stay byte-identical** in every real chart — it
  carries the six-label propagation, which is the catalog's actual
  API. Change it only in `_template` and copy it to every chart in
  the same commit.
- **`deployment.yaml` / `service.yaml` are a starting point charts
  are expected to outgrow.** Real workloads legitimately add probes,
  volumes, update strategies, checksum annotations (`openwebui/`) or
  replace the Deployment entirely (`postgres/`'s StatefulSet).
  Divergence there is normal — *unnecessary* forks are the drift to
  avoid: the skeleton deployment supports `command`, `args`, `env`,
  and `envFrom` as with-guarded passthroughs (rendering nothing when
  unset), so a chart that only needs those knobs should not fork at
  all. Probes stay chart-local by design — too app-specific to
  template well.

v2 exists because a 2026-07-26 survey (issue #5) found every real
chart had forked `deployment.yaml` for exactly those knobs, while two
charts sat behind an earlier skeleton improvement. If the passthrough
surface ever grows past those four fields, that is the trigger to
build `scripts/template-sync.sh` rather than extend the copy tax.

---

## What goes in each chart's `Chart.yaml` and `values.yaml`

The four **chart-level** labels live in `Chart.yaml.annotations`:

```yaml
# Chart.yaml
apiVersion: v2
name: my-service
description: One-line description
version: 0.1.0
appVersion: "1.2.3"

annotations:
  catalog.homelab/needs-sso: "false"
  catalog.homelab/llm-traffic: "false"
  catalog.homelab/wants-vector: "false"
  catalog.homelab/exposes-mcp: "false"
```

The two **release-level** labels live in `values.yaml` (overridable
per deployment):

```yaml
# values.yaml
catalog:
  tier: dev          # sandbox | dev | prod
  dataClass: internal # none | internal | secret
```

**All six labels must be present** (the four in annotations as `"true"`
or `"false"`, the two in values.yaml as one of the enum values) even
when defaulted — explicit-default > implicit-default, and downstream
automation does not have to special-case missing labels.

### Promotion flow (the dream state from `tier`)

Same chart, three deployments, different tier per namespace:

```bash
helm install chat-sandbox catalog/openwebui/ -n chat-sandbox --set catalog.tier=sandbox
helm install chat-dev     catalog/openwebui/ -n chat-dev     --set catalog.tier=dev
helm install chat-prod    catalog/openwebui/ -n chat-prod    --set catalog.tier=prod
```

Promotion is just a git commit that bumps the `--set` (or the values
override file) in the next environment. Demotion is the same in
reverse. This is the GitOps promotion pattern; ArgoCD makes it
automatic in Phase 4.

---

## Validation (planned, Phase 4)

A pre-commit hook will lint every `Chart.yaml` for:

- All six required annotations present and well-typed.
- `data-class: secret` charts have a `secrets.enc.yaml` (and the file
  is actually encrypted — no plaintext leaks).
- `exposes-mcp: true` charts have the Sentinel proxy sidecar config.

Until that lands, the discipline is on the chart author + reviewer.
