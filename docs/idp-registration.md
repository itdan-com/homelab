# Registering the Airlock door with your identity provider

*(7.8.1, ADR-008 — bring-your-own-IdP. Authentik ships as the default;
this page is for pointing the door at the company IdP you already
have. One issuer per deployment — that boundary is what keeps email a
safe person-key; see ADR-008 Decision 1.)*

The door is a standard OIDC relying party: authorization-code + PKCE
(S256), discovery from the issuer URL, RS256 id_tokens. Whatever the
vendor, you register **one web app** with:

- **Redirect URI:** `https://<door-host>/callback` (the door's
  `SENTINEL_DOOR_ORIGIN` + `/callback`)
- **Scopes:** `openid email profile`
- **Grant type:** authorization code (PKCE). Public client works;
  confidential client works too (see `SENTINEL_OIDC_CLIENT_SECRET` /
  `SENTINEL_OIDC_CLIENT_AUTH`, default `basic`).

Then, on the Sentinel host, export before running the installer:

```
SENTINEL_OIDC_ISSUER=<the issuer URL exactly as the IdP states it>
SENTINEL_OIDC_CLIENT_ID=<the app's client id>
SENTINEL_OIDC_CLIENT_SECRET=<only for a confidential client>
```

With a non-Authentik issuer the installer automatically drops the two
lab-only warts: the split-horizon transport rewrite
(`SENTINEL_OIDC_HTTP_BASE`) and the lab-CA TLS pin
(`SENTINEL_OIDC_CA_BUNDLE`) default to empty — real IdPs have real
addresses and real certificates (system trust store). Set either
explicitly only if your deployment genuinely needs it.

Values persist across installer re-runs: each knob resolves as *your
shell's export* → *the previous install's value* → *the shipped
default*, so a routine `--code` redeploy never reverts your IdP or
drops the client secret. The installer verifies it can reach the
issuer's discovery document at the end of every run and fails loudly
naming the URL and CA it used.

## Okta

- Create an **OIDC Web App** in the Okta admin console. The **org
  authorization server** (issuer `https://<org>.okta.com`) is
  sufficient — the door validates *id_tokens*, which the org AS signs
  verifiably. No API Access Management license needed.
- Issuer: `https://<org>.okta.com`. Client auth: `basic` (default).
- Groups are irrelevant to the door on purpose: authorization comes
  from Sentinel's own policy store, never token claims (ADR-005 P1).

## Microsoft Entra ID

- App registration → Web platform → redirect URI as above.
- **Use the tenant-specific issuer**
  (`https://login.microsoftonline.com/<tenant-id>/v2.0`) — never
  `/common` (its discovery document carries a literal `{tenantid}`
  placeholder that fails strict issuer validation, correctly).
- Entra often omits the `email` claim: either configure the optional
  email claim on the app, or set
  `SENTINEL_OIDC_EMAIL_CLAIM=preferred_username`. The door also falls
  back to one userinfo call before refusing (honest bound: on that
  path the email is TLS-attested rather than id_token-signature-
  attested, and only accepted when userinfo's `sub` matches the
  id_token's; the identity pin itself always comes from the signed
  token).
- The door records `oid@tid` as a recovery attribute automatically —
  Entra's `sub` is pairwise per app registration and does not survive
  re-registering the app.

## Ping Identity (PingOne / PingFederate)

- Register a web/OIDC app; the issuer is per-environment
  (`https://auth.pingone.<tld>/<envId>/as` for PingOne) — paste it
  exactly; the door discovers from the full issuer URL, so the
  path-shaped issuer is fine.
- Client auth: `basic` (default).

## Switching IdPs later (e.g. Authentik → Okta)

Every principal is pinned to `(issuer, subject)`, so a new issuer's
tokens are **refused by default** — that is the anomaly defense
working. The sanctioned path is the console's **IdP migration
window** (People pane → advanced): a passkey holder opens it naming
the new issuer, each person's next sign-in re-pins with its own audit
row, and the window closes itself (24h default). Outside a window,
nothing re-pins, ever. Three things to know before opening one:

- The issuer must match what the new IdP asserts in `iss` (trailing
  slashes are forgiven; nothing else is). A refusal during an open
  window records the window's issuer in the audit row, so a near-miss
  is visible.
- A window naming the **current** issuer is refused outright: within
  one issuer, a changed subject stays a permanent anomaly — that IS
  the re-issued-mailbox defense, and a same-issuer window would
  disable it.
- **Re-pinning is first-writer-wins at the new issuer**: whoever
  signs in as `person@company` there first captures the pin. Open the
  window only after the new IdP's accounts are provisioned and locked
  down, and keep the TTL short.

## Enterprise-Managed Authorization (EMA / ID-JAG) — zero-consent SSO

*(7.8.3, ADR-008 D5. Off by default; turn on only when the
deployment's IdP issues ID-JAGs — Okta today, others as they ship.)*

With `SENTINEL_EMA_ENABLED=1`, the door's token endpoint accepts the
`jwt-bearer` grant carrying an **ID-JAG** — a short-lived assertion
your IdP mints after the person's normal SSO sign-in, naming this
door as its audience. The client redeems it silently: no browser, no
redirect, no consent screen. The door validates it Keycloak-strict
(`typ oauth-id-jag+jwt`, your issuer's signature, exact audience,
single-use `jti`, 300s lifetime cap, the redeeming client named in
the assertion) and then — the part that matters — **joins only
through the `(issuer, subject)` pin an interactive sign-in already
established.** No just-in-time accounts, ever: a person who has never
signed in through the browser flow cannot arrive via assertion, and
the assertion's email claim never joins anything.

Knobs: `SENTINEL_EMA_ENABLED`,
`SENTINEL_EMA_MAX_ASSERTION_SECONDS` (default 300),
`SENTINEL_EMA_ALLOW_PUBLIC_CLIENTS` (default off — the grant demands
a confidential client proving itself via `private_key_jwt` with keys
from its CIMD document; this knob is the documented weakening for a
client that ships public-only).

Two interop bounds, stated plainly: the client's CIMD document must
carry its keys **inline** (`jwks`; a `jwks_uri` is refused — fail
closed), and statically registered client_ids are public-only for
this grant (no key material lives in `SENTINEL_DOOR_STATIC_CLIENTS`)
— confidential EMA means a CIMD client.

What EMA never does: mint an elevation session. `/elevate` and
`/link` stay behind the interactive browser leg — a machine-exchanged
assertion must never satisfy a human-confirm ceremony (ADR-008 D5's
hard rule).

Client reality check (2026-08-23): Anthropic's EMA client is a
waitlisted beta wired to Okta; VS Code's preview
(`mcp.enterpriseManagedAuth.idp`, policy-managed) is the one client
you can drive yourself today. The receiver is proven by the suite's
own ID-JAG signer and testable live against Okta's open playground
(`xaa.dev`, Bring-Your-Own-Resource — no account needed).

## What never changes, whatever the IdP

- Authorization (groups, tiers, windows, forbids) lives in Sentinel's
  policy store — token claims never decide anything (ADR-005 P1).
- Approvers and the kill switch are local Sentinel passkeys; an IdP
  outage or compromise cannot reach them (ADR-005 D6).
- Offboarding at the IdP does not disable the Sentinel principal by
  itself — use the People pane's sign-in switch, which kills API
  bearers AND browser sessions on their next use (and see ADR-008 D3
  for where automated reconciliation is headed). With an external
  IdP the door's tokens default down to 60 minutes for exactly this
  reason (`SENTINEL_DOOR_TOKEN_TTL_MINUTES` overrides).

## Connecting the gateway to Okta XAA — who plays which role

*(Written after the 2026-08-23 live matrix, because the topology is
the part everyone — including Okta's own samples — makes confusing.)*

Okta's XAA sample registers a **custom authorization server in Okta
for the resource side** (that is what an `xaa-files`-style AS is): in
the sample, Okta plays BOTH the identity provider AND the resource's
authorization server, because the sample has no resource AS of its
own. **This platform does.** The Sentinel door carries its own
authorization server — the same one that did CIMD and PKCE all along,
now with the jwt-bearer receiver — so the resource-side custom AS is
replaced by the door itself:

```
MCP client ──(one Okta SSO login)──► Okta org  (the ISSUER role only)
     │                                  │
     │◄── ID-JAG (aud = the door) ──────┘   [needs the XAA EA flag]
     │
     ├──► door /token  grant=jwt-bearer     [7.8.3's receiver — built]
     │◄── door person-token
     │
     └──► door /mcp ──► Cedar ladder ──► sentinel-proxy ──► MCP servers
```

What the Okta org needs, once the XAA Early-Access flag is enabled
(it is NOT self-service — email `developers@okta.com`; evidence for
the request: org `integrator-4949708`, custom AS present, no
`token-exchange` in any AS's `grant_types_supported`):

1. a **requesting app / AI agent** entry for the MCP client,
2. a **resource connection** naming the door's issuer
   (`SENTINEL_DOOR_ORIGIN`) as the resource authorization server,
3. a delegation policy for who may reach it.

No custom authorization server. No per-MCP-server Okta objects — the
door fronts every MCP server behind one resource AS, which is what
makes it a gateway.

**Where xaa.dev fits now:** it is only the interim live ID-JAG issuer
for testing the receiver while the EA request is pending (its
playground mints real assertions audienced at any AS with no
account). Once the org's flag is flipped, xaa.dev has no role at all.
