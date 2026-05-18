# Phase 8 — Cloud (DOKS via Terraform)

**Goal:** Provision a DigitalOcean Kubernetes cluster with Terraform, apply the same root ArgoCD Application so the entire `catalog/` deploys unchanged, demonstrate the cluster autoscaler, then `terraform destroy`.

**Status:** Not started. Blocked on Phase 7. **Cost-incurring — confirm cost ceiling before applying.**

---

## High-level outline

1. Write a Terraform module: VPC, DOKS cluster, node pool with autoscaling enabled, firewall.
2. `terraform plan` → review carefully → `terraform apply`.
3. Verify cluster up via `doctl kubernetes cluster kubeconfig save <name>` then `kubectl get nodes`.
4. Install ArgoCD on DOKS (same Helm chart as local).
5. Apply the root `Application` from the existing repo. Confirm full `catalog/` deploys identically.
6. Run a load test (k6, same scripts as Phase 3) to trigger the cluster autoscaler — node count should grow.
7. Capture metrics: how much did the cluster cost during the run? Where was the autoscaling bottleneck?
8. **`terraform destroy`** — non-negotiable. Verify in the DO console that zero billable resources remain.

## Open questions to resolve at the start

- DOKS node sizes: smallest pool (1 GB / 1 vCPU) to keep costs low, or a realistic 4 GB / 2 vCPU profile to actually exercise scheduling?
- DNS: a real public domain (Cloudflare-fronted), or stay on `*.cluster.local` and access via `kubectl port-forward`?
- Secrets: how does Sentinel run in the cloud variant? **It doesn't, for this phase** — Phase 8 is the cloud-portability proof of the *catalog*, not the full security stack. Sentinel running in cloud is a separate later question.

## Cost guardrails (must verify BEFORE `terraform apply`)

- Estimate hourly burn rate from the chosen node sizes + count.
- Set a personal time budget (e.g. "3 hours of cluster runtime max for this phase").
- Set a calendar reminder for `terraform destroy`.
- After destroy, log into the DO console and *visually verify* no resources remain — Terraform state can drift.

## Phase exit criteria

- The full `catalog/` runs on DOKS identically to the WSL k3d cluster.
- Cluster autoscaler added a node under load and removed it after.
- DO console shows zero billable resources after teardown.
- Phase total cost recorded in this doc's Notes section.
- `STATUS.md` updated to "Phase 8 complete — homelab build done."

## Notes captured during execution

- (empty)
