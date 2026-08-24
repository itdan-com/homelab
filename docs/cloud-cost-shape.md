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

## "Are we on Kubernetes or k3s?" — both, and it is not a compromise

**k3s IS Kubernetes.** It is a CNCF-certified conformant distribution:
same API, same manifests, same `kubectl`, same Helm charts. The
differences are packaging — one binary instead of six, lighter
defaults, SQLite or embedded etcd instead of an external cluster.
Everything in `catalog/` runs unchanged on EKS, GKE or AKS.

So this is not "we built the cheap version". It is "we built on
Kubernetes, and we are not obliged to rent a managed control plane to
run it".

## Scaling: what actually moves, and by how much

The number that matters is **concurrent**, not registered. A 20,000
person workforce typically peaks around 5–10% concurrent (1,000–2,000
people), of whom maybe 10% are mid-generation at any instant
(100–200 active streams).

| component | scales with | at 20k registered / ~1.5k concurrent |
|---|---|---|
| control plane floor (ArgoCD, Prometheus, Authentik, cert-manager, KEDA) | nothing — fixed | 2.2 vCPU / 4.9 GiB |
| OpenWebUI | concurrent chat sessions | ~4–8 replicas |
| AI gateway (Envoy) | token throughput | 3–6 replicas; Envoy proxies thousands of streams per core, so this follows bandwidth, not users |
| Sentinel door/broker | MCP calls/sec — a Cedar eval plus a row write, sub-millisecond | 1–2 (today a VM, see the gap below) |
| MCP servers | calls/sec per server | 1–3 each |
| Postgres | connections | 1 primary, read replicas only if Authentik logins spike |

**Roughly: floor plus 12–20 pods at that scale — call it 8–12 vCPU
total.** Two mid-size nodes. The platform is not what gets expensive
at 20k users; **the model bill is**, by an order of magnitude.

**The known gap:** the `threshold: 30` output-tokens/sec on the AI
gateway is a LAB number, derived from a 9B local model at ~70 tok/s.
It must be re-derived against the real backend before it means
anything in production — already in the backlog as "the scaling knobs
need one surface".

**Sentinel is the honest bottleneck**, because it is a VM rather than
a Deployment: it cannot autoscale today. Its work per call is tiny
(one policy evaluation, one insert), so a single instance goes a long
way — but the horizontal story is *shared Postgres instead of SQLite,
two or three instances behind a load balancer*, and that is unbuilt.
Worth naming now rather than discovering at 5,000 users.

## Scale-DOWN is a policy question, not a metric question

Owner, correctly: *"probably realising 5 mins to shut down a pod in
the middle of the day is a bad idea — wait for a 2k user drop, or it
being past 5pm in most places."*

Right, and today's config is naive: the HPA's scale-down stabilisation
window is the 300s default, so a lunchtime lull can shed replicas that
are needed twenty minutes later. Churn costs more than the replicas
saved — every scale-down drops warm connections and every scale-up
pays a cold start.

The fix uses what is already deployed. KEDA supports a **cron scaler**
alongside the metric one, and the two compose: the cron sets a FLOOR
by time of day, the metric scales above it. So:

- business hours in the workforce's timezones → floor of N,
- outside them → floor of 1, and the metric may still scale up for a
  batch job or a night shift,
- and a much longer scale-down stabilisation window (30–60 min) so
  capacity is shed on a trend, not a dip.

This is a values change on the existing ScaledObject, not new
machinery — it belongs with the knobs consolidation already in the
backlog.

## Provider, given the above

- **Cheapest, and it is not close:** k3s on VMs — DigitalOcean at
  $40–70/month all-in. (Hetzner is cheaper still and deliberately not
  recommended: the owner has had a bad experience, and support quality
  is worth more than €10/month on the machine your platform lives on.)
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
deployment**, and per-cluster fees multiply per customer — which is why avoiding a
per-cluster fee is a **margin** question, not a lab-frugality one: at
$73/month per customer on EKS, 100 customers is $7,300/month of pure
control-plane rent before a single workload runs.

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


## Correction (2026-08-24, via ADR-010 recon)

This doc predates ADR-009 Decision 6 and counts only TWO always-on
VMs beside the cluster (Sentinel + Mission Control operator). The
end-state topology is now THREE: **cluster + Sentinel VM + operator
VM + forge VM** (ADR-009 D6 — the git forge gets its own trust
domain so cluster-admin can't merge the agent's own PRs, and so it
survives the cluster it rebuilds). ADR-010 sizes all three as t4g
Graviton burstable (~$12/mo each on t4g.small, ~$30-40/mo for the
trust tier including one public IPv4 each for egress). The
run-vs-connect margin math below is unchanged in shape; add the
forge VM to the per-deployment fixed cost.
