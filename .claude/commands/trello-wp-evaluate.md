Evaluate the full body of work for a given WP — checking for vision drift and unaddressed cross-WP concerns.

## Usage
`/trello-wp-evaluate <WP-N>` — e.g. `/trello-wp-evaluate WP-0` or `/trello-wp-evaluate WP-1`

## What this skill does

### Phase 1 — Establish the original vision

Run in parallel:

1. **Read `docs/WORKSTREAMS.md`** — extract the WP's full section: Goal, Scope (in/out), Consumes, Produces, Depends on, and Definition of done (DoD).

2. **Read `docs/options-agent-plan.md`** — find any additional design intent, data-flow diagrams, or invariants that apply to this WP.

3. Use hardcoded board ID `6a24e3323bff555727f457b2` ("Agentic Trading System"). Do not call `list_boards`.

### Phase 2 — Collect all completed and in-review work

1. **Fetch in parallel** using the hardcoded board ID. Do not call `get_board_details`.
   - `mcp__trello__trello_search` — search for `<WP-N.M>` (e.g. `WP-0.8`) scoped to the board to find the target card. Do **not** include brackets in the query — the search API does not require them and the card names will contain the tag.
   - `mcp__trello__trello_search` — search for the parent epic `<WP-N>` (e.g. `WP-0`) scoped to the board
   - `mcp__trello__get_lists` — list IDs and names (lightweight; used to map each card's `listId` to its list name)

2. **Filter to this WP** — collect every card whose name starts with `[<WP-N>` (epic card + all sub-tasks). Include cards in any list (Done, In Review, In Progress, To-Do) and any archived (`closed: true`) cards returned by `filter: all`.

3. **For each card in "Done" or "In Review"**, find its PR:
   - Check card comments via `mcp__trello__trello_get_card_actions` (filter for `commentCard` actions).
   - If not found, check attachments via `mcp__trello__trello_get_card_attachments`.
   - Record the PR number; note cards with no PR as "no PR found".

4. **Fetch all found PRs in parallel** using:
   ```bash
   gh pr view <number> --json title,body,state,files,additions,deletions
   ```
   Also fetch the PR bodies (the "Decisions resolved" and "Summary" sections are most important). Do NOT fetch full diffs — the PR body summaries are sufficient for this evaluation.

5. **Read all other WPs' DoDs from `docs/WORKSTREAMS.md`** to understand what each sibling WP is responsible for. This is the reference for cross-WP concern detection.

### Phase 3 — Evaluate

Systematically evaluate along four axes:

**A. Vision drift**
Compare what was actually shipped (PR summaries + card acceptance criteria) against the original WORKSTREAMS.md specification for this WP. Look for:
- Scope creep: work done that was explicitly out-of-scope for this WP (note which WP it belongs to)
- Scope shrinkage: items in the Produces list or DoD that are missing from any PR
- Contract drift: the WP's Produces (typed outputs, function signatures, schemas) differ from what WORKSTREAMS.md specifies — even subtly (renamed fields, changed return types, dropped guarantees)
- Design inversion: the implementation does something the spec explicitly placed out of scope (e.g. WP-6 placing orders, WP-4 fetching data)
- Doc drift: the WP's Status line or DoD checkboxes in WORKSTREAMS.md disagree with the board/PR state, or the matching `docs/features/*.md` describes behaviour the PRs have since changed. Statuses and feature docs are part of the repo surface — flag stale ones as a recommended action (they must be corrected in-repo, per `/trello-wp-start` Phase 5.5's doc conventions)

**B. Cross-WP concerns**
Review each PR's "Decisions resolved" table and body for decisions that have downstream consequences. Flag any where:
- A contract was changed or extended but the consuming WP owner was not explicitly named as having been consulted
- A shared type, schema field, or interface was added/removed without a WP-0 PR
- An assumption about a sibling WP's behavior was baked in (e.g. "assuming WP-2 will handle X") without a ticket or comment on the sibling WP's card
- A responsibility that belongs to another WP was quietly absorbed or deferred without a corresponding ticket

**C. DoD coverage**
For each checkbox in the WP's Definition of done:
- Mark ✓ if a PR explicitly addresses it
- Mark ✗ if no PR covers it
- Mark ? if unclear from PR summaries alone

**D. Integration readiness**
Given this WP's Produces and the sibling WPs that Consume them:
- Are the produced interfaces stable enough for dependents to build against?
- Are there stubs or TODOs in the merged work that consuming WPs may not know about?

### Phase 4 — Output

Produce a structured report using exactly this template:

```
## WP-N Evaluation: <WP name>
**Cards evaluated:** <N done + M in-review>  |  **PRs reviewed:** <count>
**Vision source:** WORKSTREAMS.md §WP-N + options-agent-plan.md

---

### Vision drift
<Bullet list. Each item: [SCOPE CREEP | SCOPE SHRINK | CONTRACT DRIFT | DESIGN INVERSION] — description with PR # and card reference. If none, write "None detected.">

### Cross-WP concerns
<Bullet list. Each item: [WP-X] — description of the unaddressed concern, what decision was made, and what alignment is still needed. Name a recommended action (open a ticket on WP-X, post a comment, explicit sign-off). If none, write "None identified.">

### Definition of done coverage
| DoD item | Status | Evidence |
|----------|--------|----------|
| <item from WORKSTREAMS.md> | ✓ / ✗ / ? | <PR # or "not found"> |
…

### Integration readiness
<2–4 bullet points on whether this WP's outputs are safe for consuming WPs to depend on. Flag any stubs, TODOs, or unstable interfaces.>

### Recommended actions
<Numbered list, highest priority first. Each action must name an owner (this WP's owner, a specific sibling WP, or "lead"). If none, write "No actions required — WP is clean.">
```

## Rules

- **Read-only** — never move cards, post comments, or take any Trello/GitHub write action.
- **PR body only** — do not fetch full diffs; evaluate from PR titles, summaries, and "Decisions resolved" tables.
- **No hallucination** — all drift findings must cite a specific PR number or card name. If a PR body doesn't mention a topic, record it as "not addressed in PRs" rather than inferring.
- **Skip cards with no PR** — note them in the output header (e.g. "2 cards have no associated PR and were skipped") but do not evaluate them.
- **Use WORKSTREAMS.md as ground truth for intent** — not your general knowledge of options trading systems or software architecture.
- **If fewer than half the WP's cards are Done or In Review**, warn the user at the top: "Only N of M cards are reviewable — evaluation may be premature."
