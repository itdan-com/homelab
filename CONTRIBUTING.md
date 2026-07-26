# Contributing

Issues, discussions, and forks are welcome without paperwork.

**Code contributions require a CLA** (contributor license agreement)
granting the project owner the right to relicense contributed code.
This is deliberate: the project is AGPL-3.0 with copyright held by a
single owner, which is what keeps dual licensing (commercial AGPL
exceptions) possible. Mixed-ownership AGPL code would freeze the
license forever — every relicensing decision would need unanimous
contributor consent.

Practicalities land when the first real contribution shows up; until
then, open an issue first so we can sort the CLA before you write code.

Ground rules for changes:

- Every chart follows the catalog contract (`catalog/README.md`) —
  six labels, explicit resources, SOPS for secret-class values.
- Pinned versions only; no rolling tags. Bumps are their own commits.
- Docs live in-repo and version with the code; decisions get an ADR.
