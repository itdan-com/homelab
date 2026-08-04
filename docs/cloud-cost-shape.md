# What this platform actually costs to run, and where

Written 2026-08-04 against the **measured** footprint of the built
platform, because a pricing answer that assumes the wrong architecture
is worse than no answer.

## The uncomfortable headline

**This platform does not scale to zero, and should not.** It is a
control plane, not a request handler. Four things are always-on by
design, not by neglect:

- **ArgoCD** reconciling desired state — a GitOps controller that
  sleeps is a platform that drifts.
- **Authentik** — every SSO login, including into the platform itself.
- **Prometheus** — a monitoring gap is invisible precisely when it
  matters.
- **Sentinel** — holds the kill switch. *A kill switch that has to
  cold-start is not a kill switch.*

Advice built on "scale to zero, pay nothing at idle" is aimed at a
different kind of software. The right question here is not "how do we
reach zero" but **"how small is the floor, and what sits above it that
genuinely bursts?"**

## Measured floor

Summed resource **requests** across the running platform (what a cloud
scheduler actually reserves):

| namespace | CPU | memory |
|---|---|---|
| argocd | 500m | 1.06 GiB |
| monitoring | 550m | 1.06 GiB |
| authentik | 200m | 0.88 GiB |
| envoy-gateway-system | 270m | 0.53 GiB |
| chat (OpenWebUI) | 200m | 0.50 GiB |
| cert-manager, keda, kube-system, rest | ~490m | ~0.87 GiB |
| **total** | **2.2 vCPU** | **4.9 GiB** |

Actual consumption is far lower (~0.05 vCPU, ~1 GiB observed), but
requests are what size the machine.

**So the platform fits on one 4 vCPU / 16 GiB node with real headroom.**
That single fact drives everything below.

## The three always-on pieces, and why they are separate

1. **The cluster** — 4 vCPU / 16 GiB is comfortable; 2/8 is tight but
   works if monitoring retention is trimmed.
2. **Sentinel** — its own small VM (1 vCPU / 2 GiB). Not a
   preference: ADR-004 requires a separate trust domain, and CLAUDE.md
   forbids sharing a host with the agent, because a process there can
   reach a loopback-bound admin API.
3. **The Mission Control agent** — a third small VM, or a scheduled
   runner. It must survive the cluster being broken, which is the
   entire reason it lives outside it.

Small VMs are cheap; the *number* of them is the point. Anyone pricing
this as "one Kubernetes cluster" will be wrong by two machines.

## The insight that changes the provider answer

**Nothing here requires managed Kubernetes.** The platform is assembled
by `bootstrap.sh` onto k3s (k3d locally). On a cloud VM, k3s has **no
control-plane fee at all** — so the $73/month EKS charge, the GKE
per-cluster hour, and the AKS SLA tier are all *optional costs we can
decline*.

That inverts the usual comparison:

| shape | control plane | compute | rough monthly |
|---|---|---|---|
| k3s on one VM (+2 small) | $0 | 1×4vCPU/16GB + 2 small | **$60–110** |
| managed k8s, 1 node pool | $0–73 | same nodes | $75–190 |
| managed k8s + NAT + ALB (AWS default shape) | $73 | same | **$150–250** |

The AWS gap is mostly **fixed idle tax**: a NAT Gateway is ~$33/month
before a byte moves, a load balancer ~$20, and each public IPv4 ~$3.65.
On a platform that idles, that tax is a large fraction of the bill.

## Where our design already avoids a documented trap

The other analysis warned that SSE-based MCP servers burn compute
holding idle connections open. **We do not have that exposure**: the
Airlock door is deliberately request/response only — `GET /mcp` returns
405 — a choice made for policy reasons (nothing outlives the policy
that authorised it) that turns out to be the cheap shape too.

## One recommendation from that analysis we must NOT take

> *"Keep the MCP servers off the k8s cluster and on Cloud Run or
> Container Apps, where scale-to-zero is native."*

This would break the security model. MCP servers sit behind the
`sentinel-proxy`, and their NetworkPolicy admits **only** that proxy's
pods — that is what makes "nothing reaches an MCP server without a
capability check" true. On Cloud Run they would be reachable directly,
defended only by their own auth, which for the Slack server **fails
open when unconfigured**.

The cost saving would also be imaginary: each MCP server requests
20m CPU / 64 MiB. Twenty of them is half a vCPU. **We would be trading
the enforcement point for rounding error.** If their idle cost ever
does matter, KEDA (already deployed) scales them to zero *inside* the
cluster, with the proxy still in front.

## Provider, given the above

- **Cheapest, and it is not close:** k3s on VMs. DigitalOcean or
  Hetzner at $40–70/month all-in for the whole platform. This is the
  Phase 9 target and the right first proof.
- **Cheapest managed:** AKS — free control plane on the basic SKU and
  free cross-AZ traffic, which matters because this platform is
  egress-heavy (agents calling GitHub, registries, LLM APIs).
- **GKE Autopilot:** least operational surface, one cluster largely
  covered by the monthly credit. Good if you want to stop thinking
  about nodes.
- **AWS:** most expensive for this shape, and the NAT/ALB/IPv4 floor is
  the reason — not compute. **Build for it anyway if adopters are on
  it**, because "runs in your VPC" is worth more than $80/month, and
  the platform is provider-neutral by construction.

**These are not exclusive.** Run the product's own deployment on the
cheap shape; support customers wherever they are.

## The question that actually decides the architecture

> *Does the platform run the clusters it manages, or connect to
> clusters the customer already owns?*

Today it is unambiguously the **former**: `bootstrap.sh` assembles a
platform and Mission Control manages *that* cluster. ADR-002's "domain
in, platform out" says the same thing. So the unit of sale is **a
deployment**, and per-cluster fees multiply per customer — which is
exactly why the k3s option matters commercially, not just for the lab.

The alternative — connecting to a customer's existing cluster — is a
different product with a different security model (their RBAC, their
network, our agent inside it) and is **not** what has been built.
Worth deciding deliberately rather than drifting into.

## What genuinely bursts, and belongs on spot

The AI agent runs: bursty, minutes long, interruptible, and safe to
retry. Kubernetes Jobs on spot/preemptible nodes, queue-scaled. And
the honest note — **token spend will exceed infrastructure spend by an
order of magnitude**, so optimising the cloud bill by 20% while the
model bill grows unwatched is the wrong place to spend attention.
