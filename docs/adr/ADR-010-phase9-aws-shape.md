# ADR-010: Phase 9 — the AWS shape (k3s-on-EC2, the trust-domain VMs, and what the catalog owes the cloud)

**Status:** **Proposed** (2026-08-24), awaiting owner accept/amend.
Written from an eight-agent recon (four readers over ADR-002/004/009,
the cost-shape doc, and the catalog's portability blockers; four
researchers over EKS-vs-k3s economics, trust-domain VMs on AWS,
Terraform+GitOps handoff, and the catalog swaps) after the owner's
directive: *"need to finish this build so we can host on aws."* The
AWS lean is decision #3's recorded state; this ADR turns "aws k8s"
into a concrete, costed, portability-preserving plan.

**What this ADR decides, and what it explicitly does not:** it fixes
the compute substrate (Decision 1), the trust-domain topology on AWS
(Decision 2), the catalog's cloud-portability work (Decision 3), the
Terraform/GitOps shape (Decision 4), and secrets (Decision 5). It
does not re-open ADR-002's provider-neutral "domain in, platform out"
contract or ADR-004/ADR-009 D6's trust rules — it *implements* them
for AWS — but it does two things to prior ADRs that are named rather
than smuggled: it **amends ADR-002 Decision 1's "DOKS first" target
ordering to AWS-first** (owner decision #3), keeping DOKS as the
documented cheaper-managed fallback and ADR-002's provider-neutral
contract fully intact; and it **resolves ADR-002 Decision 2's
deliberately-deferred knob-plumbing question** (root values vs
ApplicationSet parameters — Decision 4 below). ADR-004 is formally
still *Proposed* but is treated as binding in practice by CLAUDE.md
and the shipped Phase 5.5 work; accepting ADR-010 is the natural
moment to flip ADR-004 to Accepted, and this ADR builds on its
trust rules as such.

## Context — the numbers that force the first decision

Our own `cloud-cost-shape.md` measured the platform floor at **2.2
vCPU / 4.9 GiB of requests** — and 2.2 vCPU of *requests* plus system
overhead does not admit on a 2-vCPU node, so this workload needs a
**4-vCPU node** (~$106-119/mo Graviton c7g/m7g; ~$121 Intel
t3.xlarge) — plus the always-on trust-domain VMs. All figures below
are **us-east-1 on-demand and region/date-sensitive** (NAT roughly
doubles in AP/SA); re-price against the AWS calculator at build time.
Confidence is mixed and tagged: EKS control plane $73/mo ($0.10/hr),
NAT $33/mo ($0.045/hr), IPv4 $3.65/mo are **verified** (fetched from
aws.amazon.com); ALB ~$16/mo is **community-report** (aggregator);
t4g and gp3 figures are **vendor-claim** (Vantage/Economize, not a
live AWS read).

- **EKS's fixed premium over k3s-on-EC2 is ~$122/mo of pure AWS
  plumbing** — control plane $73 + NAT $33 + ALB $16 (≈$125 once
  EKS's 2-3 billable IPs vs k3s's 1 are counted) — **none of it
  compute** (the same 4-vCPU node sits under both). Anchored to
  identical 4-vCPU Graviton nodes: **k3s-on-EC2 ≈ $110/mo** (node
  $106 + one public IPv4 $3.65, no control plane / NAT / ALB) vs
  **EKS ≈ $236/mo** all-in — roughly half, and the saved half is
  entirely managed-AWS plumbing.
- Those line items are exactly the **AWS-specific coupling a
  portability platform exists to avoid**: the control-plane fee buys a
  managed HA control plane our `git revert` + `bootstrap.sh` DR story
  already replaces (with the honest limit in Decision 1 ground 4);
  NAT and ALB are avoidable for the *cluster node's inbound path* by
  a public subnet + bundled ingress (the VMs' outbound egress is a
  separate line item, Decision 2).
- **The real managed-EKS disqualifier is the CNI lock, not the
  Gateway API.** EKS **Auto Mode manages the data plane and does not
  permit substituting Cilium** (verified) — which breaks ADR-003's
  Cilium + kube-proxy-retained + Hubble choice that Phase 5.5's
  policy-verdict auditability depends on. (An earlier draft of this
  ADR called Gateway API the disqualifier; that was wrong — our own
  recon cites AWS's guide to running Gateway API in Auto Mode *via
  Envoy Gateway*, our own data plane. Corrected: it is the CNI, and
  Auto Mode's managed LB controller separately serving only Ingress
  + Service annotations is a secondary friction, not the wall.) So
  the *low-ops* managed path (Auto Mode) is off the table for
  Cilium reasons; managed EKS that keeps Cilium means standard node
  groups + self-managed LB controller — the full-price path plus
  manual wiring. **Flagged for a live re-check** against a current
  EKS Auto Mode cluster before the EKS module is ever built.

## Decision 1 — k3s-on-EC2 is the default; EKS is a documented, unbuilt module swap

Phase 9 provisions **k3s on EC2** (a single Graviton node in a public
subnet for the cost-min tier; the sizing profiles add nodes). This is
the recommendation, on four grounds:

1. **Exact parity, zero new substrate, lighter build.** It is the
   *same k3s* the WSL2 lab runs — `bootstrap.sh` carries over, the
   Cilium/kube-proxy choice (ADR-003) carries over, "domain in,
   platform out" is literal rather than a re-platforming. Said
   plainly for eyes-open weighing: it is also simply *less
   engineering* than EKS — no IRSA, VPC-CNI, or LB-controller wiring
   to build and maintain.
2. **~Half the bill, and the saved half is lock-in.** The ~$122/mo
   EKS fixed premium buys managed features (IRSA per-pod IAM, AWS LB
   Controller, VPC CNI, managed control plane) that are precisely what
   a reference implementation built to be portable should not depend
   on.
3. **The low-ops managed path is closed by the CNI lock.** Auto Mode
   — the reason to pick EKS for convenience — cannot run Cilium
   (ADR-003), so the managed-EKS convenience story does not apply to
   our stack; the version that keeps Cilium is neither cheap nor
   low-ops.
4. **DR covers what the managed control plane sells — but state the
   real downgrade.** `git revert` + `bootstrap.sh` rebuild the whole
   platform, so a *managed* control plane is not load-bearing. The
   honest cost, drawn plainly: single-node k3s has **no workload HA
   either** — a node or AZ loss is a **full platform outage**, and
   because EBS is single-AZ + RWO (Decision 3), Postgres/OpenWebUI
   data is stranded in that AZ and recovered from an **EBS snapshot,
   not git revert**. We forgo EKS's ~99.95% control-plane SLA. This
   is *acceptable for the cost-min tier* as an explicit
   downgrade-from-managed, not because HA is unimportant; the sizing
   profiles' larger tiers run **3-server k3s** for HA, whose *premium*
   (two extra small server nodes) is comparable to EKS's ~$122/mo
   fixed overhead.

**EKS is not rejected — it is the second module.** The provider-
neutral contract (ADR-002: "the catalog is identical in both,"
provider boundary is a swappable module; the crisp phrase "one
Terraform module per provider" is ADR-004's) means a customer who
*needs* EKS (compliance requiring managed control plane, existing AWS
IAM investment, an SLA) gets it by writing the EKS module against the
same catalog — and the honest cost of that path (the Cilium/Auto-Mode
lock, the ~$122/mo, the self-managed LB controller + Cilium-chaining
wiring) is documented here so the choice is eyes-open. This ADR builds
the k3s-on-EC2 module; the EKS module is named future work, not a gap.

**DOKS stays the parity proof it always was.** phase-09-cloud.md
targeted DOKS precisely because it is the cheapest managed control
plane with the least lock-in — a genuinely good option if "managed"
is the deciding value. AWS-first is the owner's call (decision #3);
DOKS remains the documented fallback and the second proof that the
catalog is provider-neutral.

## Decision 2 — the trust-domain tier: three tiny VMs, one-way trust, tunnel-only admin

ADR-004 and ADR-009 D6 already decided the *shape*: the cluster node
plus **three non-cluster VMs** — Sentinel, operator, forge — none
sharing the kill switch's host (four machines counting the cluster).
This ADR makes it AWS-concrete, and it is cheap — **~$30-40/mo for
all three** (t4g Graviton burstable, all idle-mostly — the textbook
burstable workload): **Sentinel and forge on t4g.small** (~$12/mo
each, 2 GiB), **operator on t4g.small** too (~$12/mo — `cloud-cost-
shape.md` specs it at 1 vCPU / 2 GiB, and 1 GiB t4g.micro would risk
OOM'ing the Agent-SDK runtime + kubectl + git checkout, silently
blinding Mission Control — the exact failure ADR-009 works to make
loud), plus **one public IPv4 each** (~$3.65/mo) for outbound egress.

- **Egress, accounted for (it does not break Decision 1's NAT
  saving).** All three VMs need outbound internet — cloud-init clones
  the repo, apt installs, the SSM agent dials home, and the forge
  push-mirrors to github.com (ADR-009 D6). They get it via a **public
  IP + egress-only security group** (the ~$3.65/mo/IP above), NOT a
  NAT gateway: Decision 1's "NAT avoidable" is about the cluster
  node's *inbound* path and holds; the VMs' *outbound* need is priced
  here as three IPs, not a re-incurred $33/mo NAT.
- **One-way trust is native to AWS security groups.** The broker VM's
  SG gets ONE inbound rule whose *source is the cluster's SG* (not a
  CIDR), on ONLY the mTLS capability-check port. SGs are directional
  and stateful, so the cluster reaching the broker grants the broker
  no path back — the asymmetry CLAUDE.md mandates, enforced by the
  cloud primitive. The forge's SG opens only its **git-serving** port
  to the cluster SG (so ArgoCD can pull), never its admin port.
- **The admin API never touches the network — and the SSM path is
  locked to a human.** Sentinel's admin API + kill switch (and the
  forge's admin UI) bind loopback on the VM exactly as on WSL2, so
  even a misconfigured SG cannot expose them, and are reached only by
  **SSM Session Manager port-forwarding**: no public IP for admin, no
  inbound port, no bastion, every session logged. But SSM is a
  network path gated by *IAM*, not by physical unreachability the way
  WSL2 loopback is — so the load-bearing invariant, stated and
  enforced: **`ssm:StartSession` against the Sentinel and forge
  instances is granted ONLY to the owner's human IAM principal; every
  workload, agent, and cluster role is denied it** (deny-by-default
  plus an explicit deny for defense in depth). Without that deny,
  "the cluster has no path to the admin API" would be asserted, not
  secured. This is why the IAM that grants it lives in the
  agent-unreachable Terraform (Decision 4), not where the operator
  can PR it. Chosen over Tailscale precisely because Tailscale would
  put the admin API on a mesh the agent host also joins.
- **The same installer runs, unchanged.** cloud-init clones the repo
  at a pinned ref and execs the existing `install-systemd.sh` /
  `install-tick.sh` / `install-mirror.sh`; Terraform's `templatefile`
  injects only detected host values (the VM's private IP, the
  cluster's SG id, DNS names) into the env file the units read — the
  ADR-004 anti-drift rule, honored: Terraform points at the process,
  it does not re-create it. `user_data_replace_on_change = true` so a
  bootstrap edit replaces the instance rather than silently no-op'ing.
- **The forge (ADR-009 D6) lands here as the fourth deliverable** —
  its own t4g.small, Gitea/Forgejo with branch-protection-as-code and
  the author≠approver negative test D6 requires, one-way push mirror
  to github.com, ArgoCD pointed at it. The bare mirror built in
  ADR-009 D3 is its seed. Co-location with the operator VM stays the
  named cost option with D6's stated conditions; the fourth VM is the
  default.

## Decision 3 — what the catalog owes the cloud (the real work list)

The recon confirmed the catalog carries over more cleanly than the
*edge* does. Four concrete changes, three of them the ADR-002 knobs
finally getting built:

1. **Storage — the biggest change, and NOT just `storageClassName`.**
   OpenWebUI and Postgres use hand-rolled **hostPath PVs** pinned by
   `volumeName` with `storageClassName:""` to bypass k3s's provisioner
   — none of that exists on EBS. These must become **dynamic PVCs**
   (delete the PV templates, drop `volumeName`) against a gp3
   StorageClass (~$0.08/GB-mo, vendor-claim). Loki (20Gi), Prometheus
   (5Gi), and Grafana (1Gi) hardcode `local-path` and need only the
   class swapped. Two EBS
   constraints the hostPath model hid, stated so they are not
   surprises: EBS is **single-AZ and ReadWriteOnce** (a pod can't
   reschedule cross-AZ; RWX needs EFS), and the "survives cluster
   delete" property becomes `reclaimPolicy: Retain` + EBS snapshots.
   *(On k3s-on-EC2 the same EBS CSI + gp3 class applies — this work is
   not EKS-specific; local-path is the lab-only thing.)*
2. **Ingress + DNS + TLS — the edge, and the current fact matters.**
   Today **door TLS terminates at Traefik**, not Envoy: every human
   door is a Traefik `IngressRoute` on the `websecure` entrypoint with
   `tls.secretName` ("TLS terminates at Traefik" is the templates' own
   comment), while the Envoy AI Gateway listens on plain HTTP :80,
   ClusterIP, terminating no edge TLS at all. So the cloud edge is not
   "swap the issuer and done" — it is the load-bearing work: **(a)
   migrate the door charts from Traefik IngressRoute to Envoy
   HTTPRoute AND stand up TLS termination at an Envoy Gateway listener
   that does not exist today** (this is item 3's migration, which item
   2 depends on), then **(b)** put that Gateway behind a cloud
   LoadBalancer Service with the wildcard cert minted in-cluster by
   **cert-manager + Let's Encrypt DNS-01 via Route53** — the portable
   path (the issuer swap from lab-CA to Let's Encrypt is the *small*
   part; the Traefik→Envoy edge migration is the large part). ACM-on-
   ALB is the lower-ops AWS-native alternative, rejected as default:
   AWS-locked, terminates only at the ALB, yields no reusable k8s TLS
   secret. The `*.lab.local` CoreDNS shim becomes real DNS on the
   owner's domain; the `domain`, `issuer`, `externalPort` knobs get
   their global home here.
3. **The Traefik-IngressRoute migration (ADR-004 gap 1).** Every human
   door is a Traefik `IngressRoute` CRD — **7 charts** define one
   today (echo, openwebui, authentik, loki, monitoring/grafana,
   argocd, sentinel-proxy; ADR-004's "eleven" is a stale 2026-07-27
   count, corrected here). Traefik arrives free with k3s, so on
   k3s-on-EC2 these *still work* — but the right fix, and the one that
   makes EKS/DOKS possible AND provides the Envoy TLS listener item 2
   needs, is **migrating the doors to Gateway API HTTPRoute** on the
   Envoy Gateway. One gateway implementation, no bundled-Traefik
   dependency. This is **its own named sub-session** (it touches 7
   charts and stands up a new TLS listener) — not "sequenced so it
   doesn't block stand-up," because that phrasing would let the
   EKS/DOKS-enabling work quietly slip.
4. **Cilium (ADR-003) carries — restate the rationale for the node
   shape.** On k3s-on-EC2 the lab's Cilium install is unchanged. The
   IPAM pool `10.42.0.0/16` must not collide with the VPC CIDR (a
   Terraform-level check). *If* the EKS module is ever built, ADR-003's
   kube-proxy-retained choice maps to **Cilium in VPC-CNI chaining
   mode**, and Auto Mode is off the table (it manages the CNI) — noted
   for that module, not this one.

**KEDA and the Envoy AI Gateway carry to the cloud unchanged** (both
self-contained pods+CRDs; KEDA's Prometheus scaler works identically
once kube-prometheus-stack is present) — confirmed, no work owed.

## Decision 4 — Terraform stands up the substrate; ArgoCD owns everything above it

The **GitOps Bridge** pattern, which is exactly ADR-002's promise made
mechanical: Terraform provisions VPC + the k3s node(s) + the three
trust VMs, installs ArgoCD via Helm, writes cluster metadata (repo
URL, domain, issuer, storageClass, externalPort — the four knobs) onto
the ArgoCD cluster Secret, and applies the root ApplicationSet. From
there ArgoCD converges `catalog/` — the same app-of-apps that runs in
the lab, **after the Decision 3 portability changes** (dynamic PVCs,
HTTPRoute doors, the global knobs); the components that are genuinely
unchanged are KEDA and the Envoy AI Gateway. "Same catalog," not
"untouched catalog."

- **Terraform's k8s footprint stops at "install ArgoCD + seed the root
  app."** It must NOT manage in-cluster workloads through the
  kubernetes/helm providers — that couples app lifecycle to TF state
  and is the top cause of destroy-time hangs. ArgoCD owns catalog
  state; Terraform owns substrate.
- **The four-knob plumbing decision ADR-002 left open** gets resolved
  here: the knobs travel as **ApplicationSet parameters injected from
  the cluster Secret** (the GitOps Bridge mechanism), not a
  committed-per-environment root values file — because the values are
  environment-specific and some (the domain, the LB address) are only
  known after `terraform apply`. **This is net-new templating, not a
  drop-in:** today's `catalog/argocd` ApplicationSet is a pure
  git-file generator whose values come from each chart's own
  `values.yaml` — it has no cluster generator and no path for
  cluster-Secret parameters to reach the templated Applications.
  Resolving ADR-002 D2 means converting it to a **matrix (git ×
  clusters) generator** with the four knobs templated into each app's
  `source.helm` block, and wiring every chart's knob through — its
  own sub-session (9.2), not a free consequence of Terraform.
- **`infra/` is born here, split by trust domain — this is a
  correctness requirement, not organization.** The Mission Control
  operator opens PRs against *this whole repo* (its GitHub App is
  repo-scoped, not path-scoped — fine-grained tokens cannot restrict
  by path). So Terraform that **defines the trust boundary** — the
  one-way-trust security groups, the SSM/IAM that gates the
  kill-switch host, the deny of `ssm:StartSession` to every non-human
  principal, the trust-VM topology — **must NOT live where the agent
  can PR it**, or it reconstitutes ADR-004 reason 1's "a routine PR
  that doesn't look dangerous" self-approval hole exactly (the same
  reason ADR-005 D5 keeps the policy store in an agent-unreachable
  location). Layout: the ordinary substrate (`infra/modules/{network,
  k3s-node}/`, `infra/aws/`) lives in-repo; the **trust-boundary
  modules (`trust-vms`, `forge` SGs + IAM) live in a separate repo or
  location the operator's token cannot reach**, applied by the owner,
  never by a Mission Control PR. Named as an invariant here so 9.1
  builds it that way rather than discovering it later.
- **Destroy hygiene is a first-class script, not an afterthought.**
  `terraform destroy` only removes state-tracked resources; in-cluster
  LoadBalancer Services and PVCs create ALB/NLB/EBS out of band that
  orphan and block VPC deletion. The teardown path uninstalls the
  ArgoCD apps (releasing LBs/PVCs), waits, then destroys — and a
  post-destroy check enumerates leaked LBs/volumes/EIPs/log groups.
  This is the per-second-billing "destroy when done" rule (ADR-002)
  made safe, and it is part of the deliverable, tested by actually
  running it.

## Decision 5 — secrets: dual-recipient SOPS (age + KMS), value-in-by-console

Two distinct secret domains, not one — the draft conflated them:

- **Cluster/catalog secrets** (the `catalog/*/secrets.enc.yaml` files
  committed to git, decrypted by the repo-server via helm-secrets):
  keep **SOPS+age** for local development and **add an AWS KMS
  recipient** to `.sops.yaml` so the same files decrypt in cloud via
  the cluster's workload identity (IRSA on EKS, node role on
  k3s-on-EC2) reaching `kms:Decrypt` — no age private key in-cluster
  on that path, sidestepping the age bootstrap chicken-egg. This is
  the ADR-002 secrets spike resolved. There is no console paste here —
  cluster secrets live encrypted in git, always have.
- **Sentinel host secrets** (upstream credentials, per-person tokens —
  the values a human enters at the Connections screen today): these
  follow **ADR-004's model** — an AWS Secrets Manager / Parameter
  Store container, fetched at use by the VM's **instance role** (never
  a copied credential), the value **never in Terraform state or a
  container env var**. `terraform apply` creates the secret's
  container and access policy; the value goes in **through the
  console, exactly as the lab does today**. Sentinel's own mTLS PKI
  stays out-of-git and host-minted (`mint-certs.sh`), now run by
  cloud-init on the Sentinel VM.

## Sizing profiles (phase-09-cloud.md, honored)

The 150/500/1k/2.5k/5k/10k/15k-person tiers stay the phase's
deliverable, unchanged in intent: each pins node count/size, gateway
replica floor/ceiling, KEDA thresholds, and DB sizing; k6 validates up
to the 1000-person tier (cost-gated, 500 the floor goal); larger tiers
ship as labeled extrapolations; **scale-DOWN to near-idle cost is half
the test.** Two honest notes the cost-shape doc already flagged: the
gateway's `threshold: 30` tok/s is a lab number (9B local model) that
must be re-derived against the real backend, and **Sentinel is the
scaling bottleneck** (a VM, not a Deployment — its horizontal story is
shared-Postgres-instead-of-SQLite + instances behind an LB, which is
unbuilt and out of scope here).

## Consequences

- Phase 9 ships: the `infra/` Terraform tree (substrate in-repo,
  trust-boundary modules agent-unreachable), a k3s-on-EC2 cluster
  running the catalog after its Decision-3 changes, the three
  trust-domain VMs with the same installers, the forge (D6), the
  four-knob matrix-generator plumbing, real DNS+TLS, the destroy
  path, and the sizing-profile k6 runs. **This is a multi-session
  phase (5+), not one that "likely splits":** (9.1) network + k3s
  node + ArgoCD handoff + the agent-unreachable trust-boundary
  Terraform; (9.2) the matrix-generator knob wiring + storage
  (dynamic PVCs); (9.3) the **Traefik→HTTPRoute door migration + the
  new Envoy TLS listener** — its own session, it touches 7 charts;
  (9.4) trust VMs + forge; (9.5) the **sizing-profile k6 campaign** —
  scale-up AND scale-down across the 150/500/1000 tiers on live
  cloud, itself several sittings — plus the destroy proof. The public
  k3s node needs its own hardening in 9.1 (see below).
- **The public k3s node's inbound surface must be locked** — the
  cost-min design puts it in a public subnet (to avoid NAT), which
  exposes the API server (6443) and any hostPort unless SG-restricted.
  Rule set: **443 from the world** (ingress), **6443 only from the
  owner's known source / via SSM**, **SSH closed (SSM only)**,
  everything else denied. The platform's trust-domain rigor must
  extend to the cluster node itself, not just the VMs.
- The catalog gains cloud-portability it lacked (dynamic PVCs,
  Gateway API doors, the global knobs) — which also improves the lab
  (the hostPath PVs and bundled-Traefik dependency were always latent
  debt).
- `cloud-cost-shape.md` gets the third VM (the forge) it never
  recorded — a small doc fix flagged by the recon.
- The "run vs connect" question (does the platform run clusters or
  connect to customers') stays open and is made sharper by the
  numbers: at k3s-on-EC2's ~$110/mo-per-deployment (node + IPs, plus
  the ~$30-40/mo trust VMs) the run model is
  viable; EKS's $73/mo control-plane-per-customer is where it would
  hurt at scale. Named, not decided here.

## Alternatives considered

- **Managed EKS (Auto Mode).** Rejected as default: it manages the
  data plane and cannot run Cilium (ADR-003), and still pays the $73
  control plane. The managed convenience it sells is the one thing
  our Cilium-dependent stack can't use. (Not the Gateway API — an
  earlier draft claimed that; corrected, our own recon shows Gateway
  API runs on Auto Mode via Envoy Gateway.)
- **Managed EKS (self-managed LB controller).** The real EKS option;
  deferred to the second module. Full price plus manual Cilium-
  chaining and LB wiring, bought only when a customer needs managed
  control plane + IRSA. Documented, not built.
- **DOKS.** Genuinely good and cheaper-managed; kept as the parity
  fallback. AWS-first is the owner's directive.
- **ACM-on-ALB for TLS.** Lower-ops on AWS, rejected as default:
  AWS-locked, no portable k8s secret. Available to the EKS module.
- **Fargate / serverless for MCP servers.** Already rejected in
  cloud-cost-shape.md — it removes the sentinel-proxy enforcement
  point to save a rounding error of compute; KEDA-to-zero in-cluster
  is the answer if idle cost ever matters. Unchanged here.

## Non-goals

- Building the EKS or DOKS modules (named future modules).
- Sentinel horizontal scaling / multi-instance broker (its own work).
- The Ollama/GPU backend in cloud (open ADR-002 research spike; the
  gateway points at a cloud LLM or a GPU node when that lands).
- Resolving run-vs-connect.

## Sources (as verified 2026-08-24)

Repo: ADR-002 (four-knob contract, construction rule), ADR-003
(Cilium/kube-proxy), ADR-004 (trust-domain cloud shape, the six-row
cloud-artifact table, provider-neutral tables, secrets-at-rest),
ADR-009 D6 (forge topology), `docs/cloud-cost-shape.md` (the floor,
the VM sizing, the idle-tax comparison), `docs/phases/phase-09-cloud.md`
(DOKS target, sizing profiles, destroy discipline), and the catalog
portability blockers read at file:line. External, confidence-tagged in
the session record: AWS EKS/VPC/EBS/EC2 pricing pages (control plane
$0.10/hr, NAT $0.045/hr, IPv4 $0.005/hr, gp3 ~$0.08/GB, t4g.small
~$12/mo); terraform-aws-modules/{vpc,eks} v21 + the GitOps Bridge and
AWS EKS Blueprints GitOps patterns; EKS Auto-Mode-no-Gateway-API
(AWS re:Post — the decision-critical finding, flagged for a live
re-check at build time); Cilium-on-EKS chaining vs ENI; cert-manager
DNS-01/Route53 and ACM-on-ALB; SSM Session Manager port-forwarding and
SG-source-referencing for one-way trust; SOPS dual age+KMS recipients
and IRSA→KMS decrypt.
