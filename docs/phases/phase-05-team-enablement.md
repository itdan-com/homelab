# Phase 5 — Team enablement layer

**Goal (slimmed per ADR-001):** Authentik (SSO — OpenWebUI and Grafana first, Portainer later) + cert-manager + Traefik for TLS on `*.lab.local`. MinIO and the prompt-gallery/LLM-observability service (Langfuse or similar) move to **on-demand**: deploy when something concretely needs them, not by default — the Phase 2.5 gateway swap changes the LLM-observability integration anyway. After this phase a friend or teammate could be onboarded end-to-end with one SSO identity.

**Status:** Not started — unblocked 2026-07-26 (Phase 4.5 closed;
carry-in catalog-contract v2 from issue #5 landed first).

---

## High-level outline

1. Add `catalog/authentik/`; configure as OIDC provider.
2. Wire OpenWebUI, Portainer, Grafana (when present in Phase 7), and the prompt gallery to authenticate via Authentik.
3. Add `catalog/langfuse/` (or alternative); integrate the AI gateway as a tracing + cost source (Envoy AI Gateway is OTel-native).
4. Add `catalog/minio/`; configure as the upload/artifact store; create initial buckets.
5. Add `cert-manager` + the existing Traefik (k3d ships with it) for `*.lab.local` TLS via a self-signed CA.
6. Local DNS: add `/etc/hosts` entries on the Mac and Windows box pointing `openwebui.lab.local`, `portainer.lab.local`, etc. at the cluster's exposed IP.

## Open questions to resolve at the start

- LDAP/SAML alternatives to Authentik if the OIDC story gets sticky? (Keycloak is the obvious fallback; Authentik chosen for lighter footprint.)
- Langfuse vs. Helicone vs. a custom prompt gallery: Langfuse is the recommendation but verify it ingests the Envoy AI Gateway's OTel traces out of the box at the current version.
- Self-signed CA UX: every browser will warn on first visit. Document the "trust this CA" step for the Mac, since that's the daily-driver browser.

## Phase exit criteria

- A user can SSO into OpenWebUI, Portainer, Grafana (Phase 7), Langfuse, and any future service with one identity.
- An OpenWebUI conversation appears in Langfuse with cost attribution.
- An upload via OpenWebUI lands in MinIO.
- TLS works on every `*.lab.local` URL without browser-warning friction for daily use.
- `STATUS.md` updated.

## Notes captured during execution

- (empty)
