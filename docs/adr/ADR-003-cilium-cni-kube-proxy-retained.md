# ADR-003: Cilium CNI with kube-proxy retained

**Status:** Accepted, implemented 2026-07-27 (Phase 5.5 entry criterion).
**Owner input:** "cilium vs kube-proxy? …which is more scalable /
production ready / secure / easy to use?" + a hard constraint: rebuilds
must leave zero manual steps for the human.

## Context

Sentinel (Phase 5.5) requires that "MCP servers refuse traffic that did
not come through the Sentinel proxy" — a NetworkPolicy guarantee that
must be *enforced* and, just as importantly, *auditable*.

The plan said "Flannel does not enforce NetworkPolicy." The pre-rebuild
smoke test (`scripts/netpol-smoke.sh`) **disproved that shorthand**:
k3s ships an embedded kube-router policy controller alongside Flannel,
and basic v1 NetworkPolicy already blocked traffic (3/3 pre-rebuild).
What the stock stack actually lacked:

- **Verdict evidence.** No record of *which* flow was denied by *which*
  policy — a denied connection just times out. Sentinel's audit story
  needs a positive "this was DENIED" artifact.
- **Headroom.** No L7/identity-aware policy (HTTP method/path, service
  identity) — the natural next rung for scoping MCP traffic beyond
  L3/L4.
- **Cloud parity.** DigitalOcean's managed DOKS (the Phase 8 target)
  runs Cilium; local/cloud drift in the network layer would undermine
  ADR-002's "domain in, platform out" contract.

"Cilium vs kube-proxy" is a category error worth recording: kube-proxy
implements *Services* (ClusterIP → endpoints); a CNI implements *pod
networking and NetworkPolicy*. They are complementary. The real choice
is whether to ALSO enable Cilium's optional kube-proxy replacement
(eBPF service routing).

## Decision

1. **Cilium 1.19.6 as the CNI** — installed by `bootstrap.sh` (step 3),
   not the catalog: a CNI must exist before any pod (including ArgoCD)
   can run. Pinned like the k3s image; values in `k3d/cilium-values.yaml`.
2. **kube-proxy retained** (`kubeProxyReplacement: false`). This is the
   exact combo DOKS runs (parity), it is the lowest-risk path through
   k3s's svclb/Traefik plumbing, and it is **not a security
   difference** — NetworkPolicy enforcement is identical. Kube-proxy
   replacement remains a deliberate one-flag experiment
   (+ `--disable-kube-proxy` in the cluster config) for later.
3. **Hubble + relay on** from day one: per-flow policy verdicts
   (`Policy denied DROPPED`, with security identities) are the audit
   primitive Phase 5.5's proxy/NetworkPolicy work builds on. UI deferred.
4. **k3s side:** `--flannel-backend=none` + `--disable-network-policy`
   (which also removes the embedded kube-router controller — Cilium is
   now the *sole* policy enforcer), IPAM pool matched to k3s's
   `10.42.0.0/16`.
5. **Infra-kind CRDs may be bootstrap-applied** when a pre-GitOps
   component needs them (cert-manager's CRDs before ArgoCD's own TLS
   door) — rendered from the pinned chart, server-side applied; the
   app itself stays GitOps.

## Consequences

- The catalog citizenship test is now mechanical: *would it survive
  `k3d cluster delete devlab && ./bootstrap.sh`?* The DR drill that
  implemented this ADR found two failures of that test (Envoy Gateway
  and Envoy AI Gateway control planes, hand-installed in Phase 2.5)
  and converted them to `catalog/envoy-gateway` +
  `catalog/envoy-ai-gateway`.
- Trust anchors must be rebuild-stable: the lab CA persists as
  SOPS-encrypted `k3d/lab-ca.enc.yaml`, restored by bootstrap before
  cert-manager can mint a replacement (fingerprint proven identical).
- Accepted loss, documented: Grafana/Prometheus state rides node-local
  `local-path` PVCs and dies with a rebuild (config re-provisions;
  history does not).
- Async propagation is the new normal: endpoint programming and policy
  enforcement settle over seconds — verification probes assert the
  *settled* state (retry loops), never a single instant.
- k3d detail for adopters: nodes sit NotReady between cluster create
  and Cilium install; `k3d cluster create --wait` tolerates this (it
  gates on the k3s process, not node readiness).
