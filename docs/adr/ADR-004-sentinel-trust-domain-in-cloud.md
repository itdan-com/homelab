# ADR-004: Where Sentinel runs in the cloud

**Status:** **Proposed** 2026-07-27 (Phase 5.5.5). Recommendation
stated; needs owner acceptance before Phase 8 scope is fixed.
**Owner input:** "continue thinking about end state and ensure we
aren't making things more difficult than they need to be if we had just
implemented things the correct production ready scalable way the first
time."

## Context

ADR-002 committed the platform to "domain in, platform out": one
`terraform apply` plus a domain yields a working, multi-user,
TLS-and-SSO platform. Everything in `catalog/` satisfies that by
construction — it is Helm, ArgoCD applies it, DOKS runs it.

**Sentinel is the one component that cannot be in `catalog/`.** Its
entire value is that it lives in a different trust domain from the
cluster it polices: it holds the kill switch, mints the capabilities,
and keeps the canonical audit log, and `CLAUDE.md` states without
exception that the cluster has no path to its admin surface. Locally
that trust domain is "the WSL2 host" — a place that simply exists, for
free, outside k3d.

In DOKS there is no such place. ADR-002 lists five research spikes and
none of them is "where does Sentinel run", so today the honest answer
to "what happens at Phase 8" is *undefined* — and the two lazy defaults
are both wrong:

- **Put it in the cluster** (a locked namespace, a taint, an admission
  policy). This ends the architecture. Cluster-admin becomes
  kill-switch-admin, and the property every other phase depends on is
  gone. Rejected without qualification.
- **Run it on the operator's laptop.** Emotionally appealing — the
  human holds the keys — but the broker must answer an ext_authz call
  on *every* MCP request, so a closed laptop means the platform's
  external actions all fail closed. A security control with worse
  availability than the system it guards gets disabled.

## Decision (proposed)

**Sentinel is a named trust domain with two instantiations, not a host
that happens to be handy.**

1. **Local:** the WSL2 host, systemd (5.5.7). Unchanged.
2. **Cloud:** a **small always-on Linux VM in the same private network
   as the cluster, outside it**, provisioned by the same Terraform run.
   The cloud firewall allows cluster → broker on the mTLS port and
   nothing else inbound from the cluster.

**Stated as a shape, not a product** (owner challenge, 2026-07-27 —
an earlier draft of this ADR said "a DigitalOcean Droplet", which read
as a commitment this platform has not made and should not make):

| Requirement | AWS | GCP | Azure | DigitalOcean |
|---|---|---|---|---|
| Managed cluster | EKS | GKE | AKS | DOKS |
| The VM outside it | EC2 instance | Compute Engine | Linux VM | Droplet |
| Same private network | VPC + subnet | VPC | VNet | VPC |
| Private name for the broker | Route 53 private zone | Cloud DNS private zone | Private DNS zone | DO private DNS |

Everything above the provisioning layer — every chart in `catalog/`,
the units, the install script — is identical on all four. What differs
is one Terraform module per provider, which is the layer *designed* to
differ; that is how every platform product does it, and it is bounded
work rather than a re-implementation.

### Reaching the console in cloud (recommended: don't expose it)

The obvious answer is a public HTTPS endpoint protected by the passkey,
and 5.5.6 hardened the console as if that were the plan. The better
answer is to not put it on the internet at all:

**Tunnel to it.** `ssh -L 8400:127.0.0.1:8400 <sentinel-host>`, then
browse `http://localhost:8400`. The console stays loopback-bound on the
VM, so the refuse-to-start guard is satisfied unchanged; there is no
public surface, no certificate to obtain or renew, and no DNS record.
WebAuthn still works because the browser sees `localhost`, which is a
secure context and a valid Relying Party ID. A mesh VPN (WireGuard,
Tailscale) is the same idea with nicer ergonomics.

This is both *fewer moving pieces* and *less attack surface* than the
public endpoint, so it is the default. The cost is that "approve from
your phone" (the roadmap's mobile PWA) needs the public path — take
that trade when the feature is actually wanted, not before.

This keeps every property the local design already proves: one-way
trust, mTLS with Sentinel's own CA, DNS-named broker, fail-closed.

## What this design already gets right (do not rebuild)

The 5.5.4/5.5.5 work is, by luck and by rule, already cloud-shaped:

- **mTLS with Sentinel's own private CA** — deliberately not
  cert-manager, precisely so the cluster cannot mint a certificate the
  broker trusts. Identical in cloud.
- **The broker is addressed by DNS name** (`sentinel-broker.internal`),
  never an IP. Locally that name is a CoreDNS shim; in cloud it is a
  private DNS record. No chart changes.
- **NetworkPolicy is plain `networking.k8s.io/v1`** — DOKS-portable.
- **Operator identity is server-resolved** (`app/actor.py`), so 5.5.6
  WebAuthn drops in without touching a route.
- **The console is already hardened as if internet-facing** — Host
  allowlist, Origin check, CSRF header, strict CSP. In cloud these stop
  being belt-and-braces and become the actual perimeter.

## The construction rule (owner directive, 2026-07-27)

Deciding *where* Sentinel runs in cloud is worth little if the thing we
install locally has to be rebuilt to get there. The owner's directive
when choosing systemd for 5.5.7:

> "Windows is NOT its end state so we should ALWAYS be doing things for
> production in the cloud, otherwise we will be re-creating processes in
> phase 8 not just building out a small terraform change."

**systemd is not a local-only choice** — a droplet runs Ubuntu and the
same units. What *would* make Phase 8 a rewrite is baking the lab into
them. So the artifact is written for the cloud VM and the lab is one
host that runs it. Concretely, an installed-outside-the-cluster artifact
is compliant only if:

| Rule | Wrong (lab-shaped) | Right (cloud-shaped) |
|---|---|---|
| Runs as its own service user | `User=bob` | `User=sentinel`, a system account |
| Lives outside anyone's home | `/home/bob/homelab/sentinel` | `/opt/sentinel` (deployed, not run from a git checkout) |
| Data under FHS | `./sentinel-dev.db` | `/var/lib/sentinel/` |
| Config is injected, not discovered at runtime | unit runs `docker network inspect` | install step detects, writes `/etc/sentinel/sentinel.env`; the unit only reads it |
| One install path | "on WSL do X, in cloud do Y" | one idempotent script; cloud-init calls the same one |
| Secrets outside the repo | `sentinel/certs/` | `/etc/sentinel/certs/`, mode 0700 |

**The test to apply before any such item is called done:** *would Phase 8
point Terraform at this, or re-create it?* If the latter, it was built
wrong. Deferring the container image is fine — two artifacts to keep in
sync is real cost and the bugs are not worked out yet — but the systemd
artifact must already be the production one.

## What is local-only and must be named (ADR-002 rule)

| Artifact | Local form | Cloud form |
|---|---|---|
| Broker address | `k3d/coredns-custom.yaml` shim → docker gateway IP | private DNS record → droplet private IP |
| Admin reachability | binds `127.0.0.1`; loopback **is** the boundary | TLS + WebAuthn + VPN; loopback is not available |
| Cert distribution | `mint-certs.sh` runs `kubectl apply` from the host | Terraform / secret manager writes the Secret |
| Sentinel lifecycle | systemd unit on a host that already exists | Terraform-provisioned droplet + cloud-init |
| Audit durability | SQLite on the host filesystem | SQLite on a persistent volume + off-box export |

## Consequences

- **Phase 8 grows a sixth deliverable**: the Sentinel droplet, its
  firewall rules, its DNS record, and its cert bootstrap. This is the
  actionable output of this ADR — without it, `terraform apply` yields
  a platform whose security backbone is missing.
- **5.5.6 (WebAuthn) is load-bearing for cloud, not polish.** In cloud
  the admin surface must be network-reachable, at which point auth is
  the only thing left. It should not slip.
- **Sentinel inherits the platform's uptime requirements.** Fail-closed
  means Sentinel down = no external actions. That is correct for a
  security gate and must be stated, not discovered.
- **SQLite stays.** One writer, tiny data, a control plane rather than
  a workload. It is not the thing that will need rework.

## Portability gaps found while checking this claim (2026-07-27)

ADR-002 says "the entire `catalog/` deploys unchanged" in cloud. Audited
against the actual charts, **that is not true today** — and, importantly,
the gaps are *not* about which cloud. They are about **k3s-bundled things
versus things we install ourselves**, so they bite identically on DOKS,
EKS, GKE and AKS:

1. **Every door is a Traefik `IngressRoute` CRD** (`traefik.io/v1alpha1`,
   in eleven charts) and **Traefik is not in `catalog/`** — it arrives
   free with k3s as `traefik-kube-system`. On any managed cluster those
   resources fail with "no matches for kind IngressRoute". Two ways out:
   add Traefik as a catalog chart, or migrate the doors to Gateway API
   `HTTPRoute`, which needs no new component because Envoy Gateway is
   already running. The second is the better answer — one gateway
   implementation instead of two — and it is a bigger change.
2. **`storageClassName: local-path`** is k3s's provisioner and exists
   nowhere else. It must become a platform value (ADR-002 already names
   storage class as one of its knobs; the charts have not adopted it).
3. **ServiceLB / host-port assumptions.** `EnvoyProxy` is pinned to
   `ClusterIP` locally *because* k3s's ServiceLB would fight Traefik for
   ports 80/443. In cloud that reasoning inverts — a real
   LoadBalancer is what you want — so the exposure model needs a
   per-environment value rather than a comment explaining the local
   workaround.

**The general lesson worth carrying:** anything k3s hands us for free is
a hidden dependency, and hidden dependencies are exactly what makes a
"it's just Kubernetes" claim false at the worst moment. These belong in
Phase 8's scope, and they are more likely to cost time than the choice
of provider.

## Known debts this ADR does *not* resolve (recorded, not fixed)

Ranked by what they would cost to fix later rather than now:

1. **No tenant scoping.** `flows`, `capability_grants`, and
   `capability_requests` have no owner column, and `flow_id` is
   client-chosen and globally unique — so two users' `flow-1` are the
   same row. ADR-002 promises multi-user. Adding `owner_id` now is a
   day's work; after the audit log has history it is an unattributable
   backfill, because you cannot retroactively decide whose grant a 2026
   row was. **Cheapest to fix before Phase 6 puts real flows in the DB.**
2. **One client certificate for the whole Envoy fleet**, covering both
   "ask for power" and "enforce power" on one listener. Anything that
   can read that Secret inherits the ask/poll surface. The claim nonce
   (5.5.5) removes the worst consequence — token theft — but per-caller
   certs with the CN recorded in every audit row is the real answer,
   and it is a chart-topology change, not a flag.
3. **The audit log is a mutable local table** with no retention,
   rotation, export, or integrity protection. A `prev_hash` chain is
   ~10 lines today and a migration-plus-backfill later.
4. **Revocation is all-or-nothing.** The only revoke path is the global
   kill switch. With more than one user, "nuke everything" means
   operators hesitate to use the switch — the worst possible property
   for a kill switch. Per-grant and per-flow revoke should exist before
   the drastic option is the only one.
5. **Enforcement is opt-in by the guarded workload.** A fronted chart
   consents to being fronted (`networkPolicy.enabled`, its own
   ReferenceGrant), so one values flip removes the gate and ArgoCD
   converges happily. The platform-side answer is a namespace default
   plus an admission policy requiring the fronting policy for anything
   labelled `catalog.homelab/exposes-mcp: "true"`.
6. **No egress policy on fronted workloads.** The ingress allowlist
   proves who can reach a hostile MCP server, never where it can reach.

## Rejected: Sentinel behind Authentik SSO

**Asked directly (owner, 2026-07-27): "we are going to eventually add
it to authentik as an app right?" The answer is no, and it is settled
rather than deferred.** Every other workload in `catalog/` should be an
Authentik client; Sentinel is the deliberate exception this whole ADR
exists to protect, and it is worth writing down because from the
outside it looks like an oversight — the kind a future maintainer would
helpfully "fix".

Three independent reasons, each sufficient on its own:

1. **It hands the operator Claude a route to self-approval.** Authentik's
   groups are declared in `catalog/authentik/templates/oidc-blueprints.yaml`
   — in this repo, applied by ArgoCD. The Phase 4.5 operator's entire
   job is opening PRs against this repo. If Sentinel trusted Authentik,
   an ordinary-looking SSO housekeeping PR that adds an identity to an
   admin group, merged among a dozen config changes, would yield an
   identity able to approve Claude's own capability requests. Claude
   would have obtained granting power without ever touching Sentinel's
   admin surface — exactly what `CLAUDE.md`'s "if a feature seems to
   require Claude reaching the Sentinel admin surface, the design is
   wrong" rule forbids, reached by a longer road. The PR gate does not
   save us, because the dangerous PR does not look dangerous.
2. **Failure domain.** The kill switch must work precisely when the
   cluster is compromised or broken. A dead Authentik already locks
   every SSO login on this platform (the liveness gate checks for it).
   A control that is reachable only while the thing it polices is
   healthy is not a control.
3. **Impersonation.** Cluster-admin implies Authentik-admin implies
   impersonating the operator — `scripts/sso-dance.sh` uses that
   feature deliberately to test as `bob`. Correct for a chat app,
   catastrophic in front of a kill switch.

**The framing to keep:** every app on this platform keeps a local
break-glass login for the case where Authentik is down (SETUP.md Part
2). Sentinel is the app where the break-glass *is* the front door.

Also rejected: Authentik as an *additional* factor on top of the
passkey. It cannot grant access, so reason 1 weakens — but an AND-factor
means a dead cluster locks the operator out of the kill switch, which
is reason 2 unchanged.

**Accepted instead:** a **link-type application** — an authentik
application with no provider, i.e. a bookmark on the user portal
(`linkApps.sentinel` in `catalog/authentik/values.yaml`). It satisfies
the real motivation behind the question ("once we get the domain in
here I don't want to be looking for all the apps we've created")
without any trust coupling: authentik neither authenticates,
authorizes, nor proxies the destination — it renders a tile. The URL is
a value, not a constant, because the console is loopback locally and
VPN-reachable in cloud.

In cloud, an **external** IdP (one the cluster does not control) would
not suffer reason 1 or 3, and is defensible as a convenience later. It
still adds a third-party availability dependency to the kill switch, so
a locally-registered passkey must remain the primary path regardless.

**Corollary for multi-user (ties to debt 1 below):** if more than one
person should be able to approve, that is a small set of passkeys
registered directly with Sentinel — not a group synced from the
platform directory. "May you use the platform" and "may you approve an
agent's request for real-world power" are different questions, and
answering both from one directory quietly grants the kill switch to
every SSO user.

### Amendment (2026-08-02, the ADR-005 carve)

This section's "no" was written about — and remains true of — the
**admin surface**: approve, deny, kill, release. Airlock's `confirm`
(ADR-005) does not contradict it; it carves the line precisely: **who
may APPROVE stays local passkeys on the loopback console; who may
SELF-ELEVATE is an IdP-authenticated person**, decided by Cedar over
entities from the agent-unreachable policy repo, on the broker's
cluster-facing surface — never on this listener. The corollary above
("who may approve is a deliberately smaller set than who may see")
is thereby promoted from footnote to load-bearing architecture. See
ADR-005 Decision 6.

## Alternatives considered

- **Sentinel in-cluster, hardened.** Rejected: ends the trust model.
- **Sentinel on the operator's device.** Rejected: availability.
- **Managed serverless broker.** Rejected: the kill switch must not
  depend on the provider plane it may need to be used against, and it
  couples the trust anchor to one vendor's identity system.
- **Two brokers (cloud + local) sharing state.** Deferred: multi-writer
  state is exactly the complexity SQLite was chosen to avoid, and one
  droplet per platform is the honest unit.
