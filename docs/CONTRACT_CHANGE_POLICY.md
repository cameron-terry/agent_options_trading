# Contract-Change Policy

**Version:** v1.0
**Status:** Frozen as of 2026-06-07
**Applies to:** `options_agent/contracts/`

---

## What "frozen" means

The contracts module defines every shared type used across all work packages. After
WP-0 is complete, the contracts are frozen: any PR that touches `options_agent/contracts/`
is treated as a public API change and must follow this process. Silent contract drift is
the single biggest risk to a parallel build — a type mismatch caught at runtime is far
more expensive than one caught at review.

---

## Change classification

| Class | Definition | Examples |
|---|---|---|
| **Additive** | New optional field on an existing model; new model that nothing yet imports | Adding `Optional[str] = None` field to `TradeProposal` |
| **Breaking** | Rename a field or model; change a field type; remove a field or model; change a required field to a different type | Renaming `conviction` → `confidence`; changing `legs: list[Leg]` to `legs: tuple[Leg, ...]` |

When in doubt, treat the change as **breaking**.

---

## Process

### Additive changes (version: minor bump, e.g. v1.0 → v1.1)

1. Open a PR against `main` with the change confined to `options_agent/contracts/`.
2. In the PR description, list every downstream WP that imports the modified model and
   confirm the new field is `Optional` with a safe default (so existing callers need no
   changes).
3. Update the **Version** header in this file (minor bump).
4. Merge when CI is green.
5. Leave a comment on the WP-0.9 Trello card noting the change and its version.

### Breaking changes (version: major bump, e.g. v1.x → v2.0)

1. Open a PR against `main`. Title must start with `[BREAKING CONTRACT]`.
2. In the PR description:
   - Name every downstream WP and file affected by the rename/removal/type change.
   - Provide a migration table: old field → new field, old type → new type.
   - Explain why the change cannot be made additive (deprecate old + add new).
3. Update the **Version** header in this file (major bump).
4. For each downstream WP that is in-flight, open a follow-up issue tagged `contract`
   describing the adaptation required.
5. After merging, leave a comment on every affected WP's Trello card linking the PR
   and describing the required adaptation.

---

## Who can propose a change

Anyone working on this project may open a contract-change PR. The author is responsible
for identifying all affected downstream WPs and completing the checklist above before
requesting review.

---

## Versioning scheme

The version number in this file tracks the contract surface, not the application version.

- **v1.x** — backwards-compatible additions to the v1.0 frozen surface.
- **v2.0** — first breaking change after freeze; resets the minor counter.
- **vN.M** — Nth breaking change series, Mth additive change within it.

The version is bumped in the same PR that makes the contract change. It is not bumped
for changes outside `options_agent/contracts/`.

---

## CODEOWNERS enforcement

GitHub requires `@cameron-terry` to approve every PR that touches `options_agent/contracts/`
(see `.github/CODEOWNERS`). This is a forcing function, not a bureaucratic step — the review
is where the downstream impact checklist gets verified.

---

## Announcement

After merging a contract change, post a comment on the WP-0.9 Trello card with:
- The new version number
- The change summary (one sentence)
- Links to any follow-up issues opened for downstream WPs
