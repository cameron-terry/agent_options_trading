Create Trello tickets for a work package (WP) defined in `docs/WORKSTREAMS.md`.

## Usage
`/trello-wp <WP-N>` — e.g. `/trello-wp WP-1` or `/trello-wp WP-0.5`

## What this skill does

1. **Reads** `docs/WORKSTREAMS.md` (the work breakdown) and `docs/options-agent-plan.md` (the design doc) to understand the WP's scope, consumes/produces, suggested issue breakdown, and definition of done.

2. **Finds the Trello board** named "Agentic Trading System" using `mcp__trello__list_boards`, then fetches its lists and labels with `mcp__trello__get_lists` and `mcp__trello__trello_get_board_labels`.

3. **Creates one epic card** for the WP in the To-Do list summarising: goal, scope in/out, DoD checklist, and an index of all sub-task cards with their destination lists.

   **Card naming convention:**
   - Epic card: `[WP-N] <Name> — Epic` (e.g. `[WP-2] State & Persistence — Epic`)
   - Sub-task cards: `[WP-N.x] <short title>` (e.g. `[WP-2.1] Schema + migrations`)

4. **Creates one card per suggested issue** from the WP's breakdown section, triaged into the correct list:

   - **To-Do** — scope and implementation details are both fully defined; a developer can start without any open questions.
   - **Needs Implementation Details** — the *what* is clear but the *how* (field shapes, data structures, design choices) needs to be decided before coding. Document the open questions explicitly in the card body.
   - **Needs Reqs** — requirements are incomplete or unscoped (missing defaults, toolchain choices, stakeholder decisions). Document exactly what must be decided and by whom.

5. **Card format** — every sub-task card uses this structure:
   ```
   **Parent WP:** WP-N — <name>
   **Labels:** `area:<x>` `contract` `blocker` (as applicable)
   **Consumes:** <WP-0 types this uses>
   **Produces:** <function/output this delivers>
   **Depends on:** <WP / sub-issue ids, or "nothing">

   ## Description
   <what and why, 2–4 sentences>

   ## [Needs implementation details | Needs reqs]   ← only if not To-Do
   <bulleted open questions — be specific, not vague>

   ## Acceptance criteria
   - [ ] ...
   - [ ] tests added and green

   ## Out of scope
   - ...
   ```

6. **Labels** — document the intended labels (`contract`, `blocker`, `area:*` from `WORKSTREAMS.md`) in each card's header.

## Triage rules

When deciding where a card goes, apply these checks in order:

1. Is there a complete field-level spec in `options-agent-plan.md` for every type/function this card produces? → **To-Do**
2. Is the deliverable clear but design choices are needed (data shapes, storage strategy, error representation)? → **Needs Implementation Details**
3. Are numeric defaults, toolchain choices, or external stakeholder decisions missing? → **Needs Reqs**
4. If unsure, present the ambiguity to the user as options before placing the card.

## After creating cards

- Report a summary table: card name → list it landed in, with a one-line reason.
- Note any triage calls that were ambiguous and explain the reasoning.