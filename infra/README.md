# `infra/` — the AWS substrate (Phase 9, ADR-010)

Terraform that stands up the **cost-min k3s-on-EC2 substrate** so the
same `catalog/` runs on AWS ("domain in, platform out", ADR-002). This
tree provisions the substrate and hands off to ArgoCD — it does **not**
manage in-cluster workloads (ADR-010 Decision 4).

## What lives here vs. what does NOT

This is a **correctness split, not organization** (ADR-010 D4, ADR-004
reason 1). The Mission Control operator's GitHub App is repo-scoped and
cannot restrict by path, so it can open a PR against *any file in this
repo*. Therefore:

| Terraform | Lives | Why |
|---|---|---|
| **Ordinary substrate** — `modules/network`, `modules/k3s-node`, the `aws/` root | **here, in-repo** | routine infra; a bad PR here is caught by ordinary review |
| **Trust-boundary** — one-way-trust SGs, the SSM/IAM that gates the kill-switch host, `ssm:StartSession` human-only deny, the trust-VM + forge topology | **a separate owner-only repo** (this build: sibling working tree `~/homelab-trust/`) | if the operator agent could PR these, it reconstitutes ADR-004 reason 1's self-approval hole — an "innocuous" PR widening a SG to the cluster, merged among a dozen |

**Never move trust-boundary Terraform into this repo.** If a change
seems to need it, the change is wrong (CLAUDE.md's Sentinel rule).

## Layout

```
infra/
  aws/                 # root config — wires the modules, holds the tfvars
  modules/
    network/           # VPC, public subnets, IGW, route tables
    k3s-node/          # the Graviton EC2 node + locked SG + cloud-init  (next increment)
```

## Status (Phase 9.1, authoring)

- `modules/network` — **authored, `terraform validate` clean.**
- `aws/` root — **authored** (wires network; k3s-node + ArgoCD handoff
  land in the next 9.1 increment).
- `modules/k3s-node`, the ArgoCD GitOps-Bridge handoff, and the AWS
  Budgets guardrail Terraform — **not yet authored.**

## Running it (no real apply yet)

Everything here is authored to `terraform validate` / `plan` fidelity.
A real `terraform apply` waits for the owner's two-key billing handoff
and a confirmed cost ceiling (see `docs/phases/phase-09-cloud.md` →
"Credential & cost discipline"). To check the config:

```sh
cd infra/aws
terraform init
terraform validate
terraform fmt -recursive -check
# terraform plan   # needs AWS credentials (the no-billing key), later
```
