# Draft: XAA Early-Access request to developers@okta.com

Subject: XAA / Cross App Access EA enablement request — Integrator org 4949708

Hi — requesting Cross App Access (XAA) Early Access enablement for
our Integrator Free Plan org:

- Org: https://integrator-4949708.okta.com (org id 4949708)
- Use case: our self-hosted MCP gateway implements the receiving side
  of the MCP Enterprise-Managed Authorization extension
  (draft-ietf-oauth-identity-assertion-authz-grant jwt-bearer
  redemption at our own resource authorization server). We need the
  org to issue ID-JAGs via RFC 8693 token exchange so employees'
  MCP clients can reach the gateway with zero per-server consent.
- Current state: custom authorization servers exist and advertise the
  jwt-bearer grant, but no authorization server in the org advertises
  urn:ietf:params:oauth:grant-type:token-exchange, and XAA does not
  appear as a self-service feature flag.

We understand self-service EA enablement was retired 2026-06-30 —
please advise on enabling XAA for this org. Thanks!
