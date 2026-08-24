# Authentik broker mode — a customer's Okta/Entra behind the WHOLE platform

*(Phase 7.8.2, ADR-008 Decision 4. Every capability below was probed
against the deployed Authentik 2026.5.6 on 2026-08-23, not read from
vendor docs — the source types, matching modes, and SCIM support are
what THIS build actually exposes.)*

The door (Airlock) can point straight at an external IdP — that is
7.8.1, and `docs/idp-registration.md` covers it. This page is the
OTHER half: the **platform's own apps** (OpenWebUI, Grafana, ArgoCD)
are hardcoded Authentik OIDC clients and stay that way. To put a
customer's Okta or Entra behind all of them at once, you do not
rewire each app — you make Authentik a **broker**: the customer's IdP
plugs in as an Authentik *Source*, and every downstream app is
untouched.

```
customer's Okta/Entra ──(Source)──► Authentik ──(unchanged OIDC)──► OpenWebUI
                                        │                            Grafana
                                        └──(the door, if you want)   ArgoCD
                                           the platform's own apps
```

## What the deployed build actually provides (probed)

- **Dedicated source presets** for the big three:
  `provider_type` includes `okta`, `entraid`, and `azuread` — not just
  generic `openidconnect`. (Full list also has google, github, apple,
  etc.) So an Okta or Entra source is a first-class object, not a
  hand-rolled OIDC endpoint.
- **`identifier` user-matching mode is available** — pin on the
  source's stable subject, never on email. This is mandatory, not
  optional: `email_link`/`username_link` are a documented
  cross-IdP account-takeover class (a source that does not verify
  email can log an attacker into an existing user). The other modes
  exist in the dropdown; do not use the `_link` ones for a customer
  IdP.
- **`identifier` group-matching mode too** (`group_matching_mode:
  identifier | name_link | name_deny`) — same discipline for group
  sync.
- **`force_authn` is present on the SAML source** in this build
  (2026.5.6) — the earlier research had this as "future release,
  unverified"; the live OPTIONS probe shows the field. So an
  IdP-initiated re-auth requirement is configurable if a customer
  needs it.
- **Inbound SCIM is a real Source** (`/api/v3/sources/scim/` → 200):
  Okta/Entra can push users and groups INTO Authentik over
  `/v2/Users` and `/v2/Groups`, which is the offboarding answer JIT
  login cannot provide.

## The bounds that matter (also from the research, still true)

- **Broker mode does NOT fix the P1 assurance bound for Airlock.**
  Authentik stays in-cluster and agent-PRable, so for AIRLOCK
  specifically the door pointing at the external IdP DIRECTLY (7.8.1)
  is the stronger posture. Broker mode is for the platform's OTHER
  apps, where the P1 bound never applied. The two compose: door →
  customer IdP directly, portal apps → Authentik brokering the same
  IdP.
- **Upstream logout does not propagate** into Authentik — a source
  session can outlive the customer's Okta session. Compensate with
  short Authentik session lifetimes; inbound SCIM deprovision is the
  durable answer.
- **The SCIM source token is a tenant-wide provisioning credential**
  (matches userName across the whole Authentik tenant, hard-deletes
  objects). For a single-customer deployment this is fine; a
  multi-customer Authentik would need isolation this build's SCIM
  source does not give — which is another reason one-issuer-per-
  deployment (ADR-008 D1) is the shape.

## Wiring it (config-as-code, per ADR-002)

Sources are Authentik blueprint objects, same as the OIDC providers
already in `catalog/authentik/templates/oidc-blueprints.yaml`. A
customer-IdP source is a new `authentik_sources_oauth.oauthsource`
(or `.samlsource`) blueprint entry with `provider_type: okta`,
`user_matching_mode: identifier`, the customer's client id/secret via
SOPS→`!Env`, and a policy binding controlling who may use it — the
same pattern the app providers use, so it deploys headless and
survives a rebuild. Left as a template-when-a-real-customer-exists:
the concrete blueprint wants a real customer's endpoints, and adding
a source that points nowhere is the "declare a dependency before it
exists" trap Phase 8 already paid for.
