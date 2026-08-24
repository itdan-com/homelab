# Phase 9 — Cloud (AWS, k3s-on-EC2 via Terraform)

**Binding decision: `docs/adr/ADR-010-phase9-aws-shape.md` (ACCEPTED
2026-08-23).** This doc is the executable checklist; ADR-010 is the
*why*. The pre-ADR DOKS version of this file is superseded — DOKS
survives only as the documented parity fallback (ADR-010 Decision 1).

**Goal:** "Domain in, platform out" on AWS. Terraform provisions the
substrate (VPC + a k3s-on-EC2 node + the three trust-domain VMs +
forge), installs ArgoCD, and hands off; ArgoCD converges the same
`catalog/` — *after* the Decision-3 portability changes — from one
entry command. Then a scale-up/scale-down k6 campaign across the
sizing tiers, then a safe `terraform destroy`.

**Substrate:** k3s-on-EC2 (ADR-010 D1), a single 4-vCPU Graviton node
in a public subnet for the cost-min tier (~$110/mo cluster + ~$30-40/mo
trust VMs). EKS is the documented, unbuilt second module. The catalog
is provider-neutral (ADR-002); the AWS boundary is one Terraform
module set.

**Status:** **9.1 in progress (authoring, no cloud account).** Phases
1-8 complete; both gating ADRs accepted 2026-08-23. All authoring is
`terraform fmt`/`init`/`validate` fidelity until the owner's two-key
billing handoff (see "Credential & cost discipline" below). **No real
`terraform apply` runs without the owner at the console and a confirmed
cost ceiling.**

---

## Hard invariants (do not violate while building)

- **Trust-boundary Terraform is agent-unreachable (ADR-010 D4, ADR-004
  reason 1).** The Mission Control operator's GitHub App is repo-scoped
  and *cannot* restrict by path, so anything that defines the trust
  boundary — the one-way-trust security groups, the SSM/IAM that gates
  the kill-switch host, the `ssm:StartSession` deny for non-human
  principals, the trust-VM + forge topology — **must not live in this
  repo.** It lives in a **separate owner-only repo** (this build uses a
  sibling working tree `~/homelab-trust/`, its own git repo, never
  pushed to `itdan-com/homelab`). Ordinary substrate
  (`infra/modules/{network,k3s-node}`, `infra/aws/`) lives here in-repo.
- **Terraform stops at "install ArgoCD + seed the root app" (ADR-010
  D4).** It must NOT manage in-cluster workloads through the
  kubernetes/helm providers — that couples app lifecycle to TF state
  and is the top cause of destroy-time hangs. ArgoCD owns catalog
  state; Terraform owns substrate.
- **The public k3s node's inbound surface is locked (ADR-010
  Consequences):** 443 from the world, 6443 only from the owner's known
  source / via SSM, SSH closed (SSM only), everything else denied.
- **The admin path is SSM-tunnel-only and human-locked (ADR-010 D2).**
  Sentinel's admin API + kill switch bind loopback on the VM; reached
  only by SSM Session Manager port-forward; `ssm:StartSession` on the
  Sentinel/forge instances granted ONLY to the owner's human IAM
  principal, every workload/agent/cluster role explicitly denied.
- **Installers are pointed at, not re-created (ADR-004 anti-drift).**
  cloud-init clones the repo at a pinned ref and execs the existing
  `install-systemd.sh` / `install-tick.sh` / `install-mirror.sh`;
  `templatefile` injects only detected host values into the env file.
  `user_data_replace_on_change = true`.

## Credential & cost discipline (owner-stated two-key plan)

1. Owner creates **AWS Budgets + billing guardrails** with a privileged
   key (authored here as Terraform the owner applies; I never hold that
   key).
2. Owner hands over a **scoped key that CANNOT change billing**.
3. I author + `terraform validate`/`plan` everything with that key —
   **no real apply** until the owner is at the console with a confirmed
   cost ceiling.
4. Every cost number in ADR-010 is us-east-1/date-sensitive — re-price
   against the AWS calculator at apply time.
5. Destroy hygiene is a first-class, tested script (9.5), not an
   afterthought: uninstall ArgoCD apps (release LBs/PVCs) → wait →
   destroy → post-destroy leak check (LBs/volumes/EIPs/log groups).

---

## Sub-sessions (ADR-010 Consequences — this is a 5+ session phase)

### 9.1 — substrate + handoff + trust-boundary Terraform  ← current
- [ ] `infra/` tree born, split by trust domain (ordinary in-repo;
      trust-boundary in `~/homelab-trust/`).
- [ ] `infra/modules/network` — VPC, public subnet(s), IGW, route
      tables; VPC CIDR chosen to NOT collide with Cilium IPAM
      `10.42.0.0/16` (ADR-010 D3.4).
- [ ] `infra/modules/k3s-node` — the Graviton EC2 node, gp3 root,
      cloud-init that installs k3s + Cilium + runs `bootstrap.sh`, the
      locked inbound SG (443/6443-restricted/no-SSH).
- [ ] ArgoCD GitOps-Bridge handoff — Terraform installs ArgoCD (Helm),
      writes the four knobs (repo URL, domain, issuer, storageClass,
      externalPort) onto the ArgoCD cluster Secret, applies the root
      ApplicationSet. (The ApplicationSet *conversion* to a matrix
      generator is 9.2 — 9.1 stands up the handoff mechanism.)
- [ ] Trust-boundary modules in `~/homelab-trust/`: `trust-vms` +
      `forge` SGs/IAM, the `ssm:StartSession` human-only IAM, one-way
      SG source references. Authored, never applied by an agent.
- [ ] AWS Budgets / billing-guardrail Terraform (owner-applied).
- [ ] `terraform fmt` + `init` + `validate` clean on every module.

### 9.2 — knob wiring + storage
- [ ] Convert `catalog/argocd` ApplicationSet to a **matrix (git ×
      clusters) generator**; template the four knobs into each app's
      `source.helm`; wire every chart's knob through (net-new, ADR-010
      D4).
- [ ] Storage: delete the hand-rolled hostPath PV templates + drop
      `volumeName` on OpenWebUI & Postgres → **dynamic PVCs** on a gp3
      StorageClass; swap `local-path` → gp3 on Loki/Prometheus/Grafana.
      `reclaimPolicy: Retain` + EBS-snapshot for the "survives cluster
      delete" property. Note EBS single-AZ + RWO constraints.

### 9.3 — the edge: Traefik→HTTPRoute door migration + Envoy TLS listener
- [ ] Migrate **7 charts** (echo, openwebui, authentik, loki,
      monitoring/grafana, argocd, sentinel-proxy) from Traefik
      `IngressRoute` to Gateway API `HTTPRoute` on the Envoy Gateway.
- [ ] Stand up a **TLS-terminating Envoy Gateway listener** (does not
      exist today — the gateway listens plain HTTP :80 now).
- [ ] Gateway behind a cloud LoadBalancer Service; wildcard cert via
      **cert-manager + Let's Encrypt DNS-01 (Route53)**; real DNS on the
      owner's domain replacing the `*.lab.local` CoreDNS shim.

### 9.4 — trust VMs + forge
- [ ] Sentinel, operator, forge VMs (t4g.small each) via cloud-init +
      the existing installers; per-VM public IP for egress (no NAT).
- [ ] Forge (ADR-009 D6): Gitea/Forgejo, branch-protection-as-code +
      the author≠approver **negative test**, one-way push mirror to
      github.com (seeded by ADR-009 D3's bare mirror), ArgoCD repointed.
- [ ] Sentinel host secrets via Secrets Manager/Parameter Store, fetched
      by instance role, value entered through the console (ADR-010 D5).
- [ ] Dual-recipient SOPS: add an **AWS KMS recipient** to `.sops.yaml`
      alongside age (ADR-010 D5).

### 9.5 — sizing-profile k6 campaign + destroy proof
- [ ] k6 scale-**up** AND scale-**down** across the **150 / 500 / 1000**
      tiers on live cloud (500 the floor goal, 1000 cost-gated); larger
      tiers ship as labeled extrapolations.
- [ ] Re-derive the gateway KEDA `threshold: 30` tok/s against the real
      cloud backend (lab number was a 9B local model).
- [ ] Run the **destroy** path end-to-end; verify zero billable
      resources in the console.

## Sizing profiles — "pick your company size" (owner ask 2026-08-02)

An adopter picks a **company-size profile** and the platform deploys
pre-sized. Tiers: **150 / 500 / 1000 / 2500 / 5000 / 10000 / 15000
people.** Each pins node count/size, gateway replica floor/ceiling,
KEDA thresholds, model-serving replicas, DB sizing. Validated by real
k6 up to the 1000-person tier (cost-gated; 500 the floor goal);
larger tiers are labeled extrapolations. **Scale-DOWN to near-idle
cost is half the test** — ultra-low idle spend is a product property.
Depends on the four-knob work (9.2) landing first. Note (ADR-011):
Sentinel's own horizontal story (N brokers + shared Postgres) is the
**enterprise-tier** deliverable, sequenced with these profiles, not
required to launch the phase.

## Phase exit criteria

- The full `catalog/` runs on k3s-on-EC2 identically to the WSL k3d
  cluster, from one entry command ("domain in, platform out").
- Real DNS + TLS on the owner's domain; every door reachable over HTTPS.
- The three trust VMs + forge run the same installers; the
  author≠approver negative test passes on the forge.
- A sizing profile proved scale-up under load AND scale-down to floor.
- `terraform destroy` + the leak check show zero billable resources.
- Phase total cost recorded in Notes below.
- `STATUS.md` updated to "Phase 9 complete — cloud parity proven."

## Notes captured during execution

- 2026-08-23 — Phase 9 started. Phase doc rewritten from the accepted
  ADR-010 (was stale DOKS content). Toolchain: terraform/aws-cli not
  present on the WSL host at start — installing user-local terraform for
  validate; aws-cli + real credentials arrive with the owner's two-key
  handoff.
