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
2. **Cloud:** a **small always-on VM outside the DOKS cluster, inside
   the same VPC** (a DigitalOcean Droplet), provisioned by the same
   Terraform run that builds the cluster. Cloud firewall allows
   cluster → broker on the mTLS port and nothing else inbound from the
   cluster; the admin surface is reachable only over the operator's
   VPN/WireGuard or an authenticated public endpoint, never from the
   VPC's cluster subnet.

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

## Alternatives considered

- **Sentinel in-cluster, hardened.** Rejected: ends the trust model.
- **Sentinel on the operator's device.** Rejected: availability.
- **Managed serverless broker.** Rejected: the kill switch must not
  depend on the provider plane it may need to be used against, and it
  couples the trust anchor to one vendor's identity system.
- **Two brokers (cloud + local) sharing state.** Deferred: multi-writer
  state is exactly the complexity SQLite was chosen to avoid, and one
  droplet per platform is the honest unit.
