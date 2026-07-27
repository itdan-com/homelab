# ADR-002: Cloud-parity contract — "domain in, platform out"

- **Date:** 2026-07-26
- **Status:** Accepted (owner direction, stated twice during Phase 5
  stage A, sharpened same day: *"this entire thing deployed from
  terraform might be the move... choose my domain in terraform and
  bam deployed stack for production"*)
- **Builds on:** ADR-001 (gateway swap, phase resequencing)

## Context

The repo is an open-source reference implementation whose credibility
is "this deploys for real, anywhere." Through Phase 5 stage A the
platform gained TLS with a deliberately swappable issuer, which
surfaced the local-vs-cloud artifact question (Windows CA import is
local-only) — and the owner set a sharper product bar:

> Deploy from GitHub, from **one terminal entry point with a
> checklist**, to either (a) the local Windows box or (b) a chosen
> cloud provider — parameters in (target, domain, credentials),
> working platform out: TLS, SSO/OAuth, multi-user, scalable, with
> multi-writer git flow that doesn't fall over. Navigating as few
> documents as possible.

Nothing in the existing plan contradicts this, but it was never
written as a contract, Phase 8 was framed as a throwaway drill, and
two near-term builds (SSO config, service doors) would silently
diverge from it if built click-first or exposure-careless.

## Decision

1. **Two deploy targets, one front door.** A single entry command
   (grown from `bootstrap.sh`) with two modes:
   - **`local`** — script-driven: prereq checks as a live checklist,
     k3d cluster, ArgoCD, then self-assembly from the catalog. Stays
     a script *on engineering merit*: WSL2/docker/hosts-file/CA-trust
     are OS-level concerns with no converging API; Terraform there
     would be a worse bash script with a state file.
   - **`cloud`** — Terraform under `infra/`: cluster (DOKS first,
     provider boundary = a module so others can follow), then the
     same ArgoCD bootstrap pointed at the same repo. Optional HCP
     Terraform workspace gives the "log in, set variables, click
     Run" experience with zero local tooling.
   The catalog is identical in both; only the infra layer differs.
2. **Global platform values contract** — four knobs, one place per
   environment: `domain` (lab.local ↔ yourdomain.com), `issuer`
   (lab-local-ca ↔ Let's Encrypt), `externalPort` (8443 ↔ 443),
   `storageClass` (hostPath ↔ provider volumes). Charts already take
   these per-service; the contract lifts them to one root-level
   values file per environment. Exact plumbing (root values vs
   ApplicationSet parameters) decided at implementation; the
   *contract* is that exactly these four knobs separate environments.
3. **Exposure policy** (drives A5 and every future door): a service
   gets a public `*.domain` door **iff humans browse it AND it
   authenticates**. Unauthenticated-by-design consoles (Prometheus,
   Alertmanager) stay port-forward/in-cluster until behind SSO
   forward-auth (Phase 7). ArgoCD's door arrives with its SSO (B7).
   APIs (AI gateway) stay in-cluster on purpose. No exceptions for
   convenience.
4. **SSO assembles headless.** Authentik configuration is
   config-as-code (blueprints) — REQUIRED, not preferred (hardens
   phase-05 B4). Clickops allowed only as exploration; the committed
   state must reproduce from git on a machine nobody clicked.
5. **DNS per environment.** Local: hosts-file entries, documented as
   the local-only artifact they are. Cloud: external-dns on the
   provided domain (or a documented single A-record/wildcard as the
   minimal path).
6. **Docs converge on one deploy document.** `DEPLOY.md`: the
   checklist, both targets, same order the entry command prints live
   — the terminal and the doc tell one story. README Quickstart
   points at it; SETUP.md stays the deep reference (GitHub App,
   secrets regeneration, gate test).
7. **Multi-writer readiness** (Phase 6 trigger): the moment proposer
   count exceeds one, turn on GitHub merge queue + required status
   checks (helm lint + rendered diff) so merges are machine-validated
   and serialized; per-service chart directories already minimize the
   conflict surface; ArgoCD converges to `main` regardless of merge
   order.
8. **Phase 8 reframed: from drill to product path.** Deliverable is
   the supported cloud deploy proven end-to-end (up on a real domain,
   real certs, SSO live, smoke-gated) — the teardown remains as cost
   hygiene after proof, and "leave it running" becomes a documented,
   supported choice rather than a rule violation.

## Research spikes (resolve before Phase 8 implementation)

- **LLM backend in cloud** — no RTX 4070 there. Options: GPU node +
  Ollama; managed inference endpoint; or split routing (local-model
  tier only exists locally; cloud runs `platform-hard` via provider
  API keys through the gateway's BackendSecurityPolicy). Cost each.
- **Let's Encrypt mode:** DNS-01 (wildcard; needs DNS-provider creds,
  pairs naturally with external-dns) vs HTTP-01 (per-host, minimal).
- **Secrets delivery in cloud:** SOPS age key as a TF-injected
  bootstrap secret vs provider KMS integration.
- **State & UX:** local tfstate vs HCP Terraform free tier (the
  login-and-parameters experience).
- **Post-deploy verification:** the `/resume` liveness gate becomes
  the deploy smoke test, printed by the entry command.

## Consequences

- (+) The open-source claim becomes checkable: one command, either
  target, verified by a gate.
- (+) Local-only artifacts are named at introduction (CA import,
  hosts entries), never silently load-bearing.
- (+) SSO/doors work built from here is cloud-correct by
  construction instead of retrofitted.
- (−) More upfront parameterization; a Terraform module tree to
  maintain; the research spikes are real work before Phase 8.
- (−) The entry command must stay honest on two targets — CI can
  only cheaply prove the render/lint layer, so the cloud path needs
  periodic live proof (Phase 8 and after major changes).
