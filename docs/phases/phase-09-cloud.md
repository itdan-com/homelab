# Phase 9 — Cloud (DOKS via Terraform)

**Goal:** Provision a DigitalOcean Kubernetes cluster with Terraform, apply the same root ArgoCD Application so the entire `catalog/` deploys unchanged, demonstrate the cluster autoscaler, then `terraform destroy`. (ADR-002 reframe, 2026-07-26: this is a supported product path — "domain in, platform out" — not a drill; the destroy is cost hygiene after proof, and keeping it up is a documented choice.)

**Status:** Not started. Blocked on Phase 8. (Renumbered 8→9 on 2026-07-31 when Airlock became Phase 7.) **Cost-incurring — confirm cost ceiling before applying.**

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

## Known blockers (2026-07-27 audit — provider-independent, k3s's fault not the cloud's)

Recorded in STATUS backlog + ADR-004; resolve before or during this phase:

1. **Every door is a Traefik `IngressRoute` CRD in eleven charts, and Traefik is not in `catalog/`** — it comes free with k3s, so on any managed Kubernetes those resources fail with "no matches for kind IngressRoute". Fix: a Traefik catalog chart, or (better) migrate doors to Gateway API `HTTPRoute` on the Envoy Gateway that already runs.
2. **`storageClassName: local-path`** is k3s-only; must become the platform value ADR-002 names.
3. **ServiceLB/host-port reasoning inverts in cloud** — `EnvoyProxy` is ClusterIP locally *because* k3s ServiceLB fights Traefik for 80/443; that local workaround is baked in as a constant.

## Open questions to resolve at the start

- DOKS node sizes: smallest pool (1 GB / 1 vCPU) to keep costs low, or a realistic 4 GB / 2 vCPU profile to actually exercise scheduling?
- DNS: a real public domain (Cloudflare-fronted), or stay on `*.cluster.local` and access via `kubectl port-forward`?
- Secrets: how does Sentinel run in the cloud variant? **It doesn't, for this phase** — Phase 9 is the cloud-portability proof of the *catalog*, not the full security stack. Sentinel's cloud shape is answered by ADR-004 (its own VPC droplet outside the cluster); implementing it is separate, later work.

## Sizing profiles — "pick your company size" (owner ask 2026-08-02)

The product bar (ADR-002: domain in, platform out) plus one knob: an
adopter picks a **company-size profile** and the platform deploys
pre-sized — a small business builds nothing and scales all their AI
tools on tested numbers. Tiers: **150 / 500 / 1000 / 2500 / 5000 /
10000 / 15000 people.** Each profile pins the platform values for
that scale: node pool size/count, gateway replica floor/ceiling,
KEDA thresholds, model-serving replicas, DB sizing.

- **Validated, not guessed:** real k6 suites run against the cloud
  deploy for the small tiers — **up to the 1000-person tier,
  cost-gated; the 500-person tier is the floor goal** (owner,
  2026-08-02). Larger tiers ship as labeled extrapolations.
- **Scale-DOWN is half the test:** each profile must also prove
  graceful contraction — quiet hours → floor replicas → near-idle
  cost. Ultra-low idle spend is a stated product property, not a
  nice-to-have.
- **Workload model per tier** defined in the k6 suite with cited
  assumptions (e.g. concurrent users ≈ 5–10% of headcount, token
  distribution per request).
- **Depends on the knobs work landing first:** the STATUS backlog
  item "scaling/threshold knobs need one obvious surface" + ADR-002's
  platform-values contract. The fewer and better-derived the knobs,
  the cheaper every profile is to define, test, and trust.

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
