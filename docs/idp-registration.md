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
