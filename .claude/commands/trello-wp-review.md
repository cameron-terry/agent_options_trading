Get all Trello tickets in "In Review" on the "Agentic Trading System" board, select the highest-priority one based on critical-path analysis, and deliver a deep code review.

## Usage
`/trello-wp-review` — no arguments needed; the skill selects the best candidate automatically.

## What this skill does

### Phase 1 — Identify the review candidate

1. **Find the board** using `mcp__trello__list_boards`.

2. **Fetch all cards and lists in parallel** with `mcp__trello__trello_get_board_cards` and `mcp__trello__get_board_details` (includeDetails: true).

3. **Filter to "In Review"** — collect only cards whose `listId` maps to the "In Review" list.
   - If zero "In Review" cards exist, report "Nothing in review right now." and stop.
   - If exactly one exists, skip scoring and go directly to Phase 2.

4. **Score each "In Review" card by critical-path impact.** Read every card on the board (not just "In Review") to build the full dependency graph from each card's `**Depends on:**` field. Then rank using these rules in order — first rule that applies wins:

   | Rank | Condition |
   |------|-----------|
   | 1 | Merging this card unblocks the most To-Do / Needs Impl Details cards downstream |
   | 2 | Merging this card unblocks at least one other card and has no unresolved upstream dependencies |
   | 3 | Merging this card resolves a single dependency chain |
   | 4 | No other cards depend on this card (standalone) |
   | 5 | Final gate — depends on most other cards |

   Ties → prefer the lower WP sub-task number (e.g. WP-0.3 before WP-0.7).

5. **Select the top-ranked card** as the review target. Output a one-line rationale for the selection before Phase 2.

### Phase 2 — Gather review context

Run all of these in parallel:

1. **Read the card description and checklist** — note the Produces, Acceptance criteria, and Depends-on entries. Use `mcp__trello__trello_get_card_checklists` if the card has checklist items.

2. **Find the PR link** — first check card comments via `mcp__trello__trello_get_card_actions` (filter for `commentCard` actions). If no PR link is found, check attachments via `mcp__trello__trello_get_card_attachments`. If still not found, report the selected card and ask the user to supply the PR URL, then stop.

3. **Read project docs** — `docs/WORKSTREAMS.md` and `docs/options-agent-plan.md`, focusing on the section for this card's parent WP (e.g. "WP-0" section for WP-0.3).

4. **Fetch the PR** using `gh pr view <number> --json title,body,state,files,additions,deletions` and `gh pr diff <number>` to read the actual diff. Read only files touched by the diff — do not scan the whole repo.

### Phase 2.5 — Run author verification steps

After Phase 2 completes and you have the PR body:

1. **Extract verification statements** — scan the PR body for lines matching the pattern `Verify <something>` (in checklist `- [ ] Verify ...` / `- [x] Verify ...` or plain `Verify ...` form). These are claims the author committed to validating.

2. **Run each verification** — for each extracted statement, write a minimal Python snippet and execute it with `uv run python -c "..."`. Each snippet should:
   - Import only what it needs from `options_agent.contracts` (or other modules touched by the diff).
   - Assert the specific claim (e.g., `assert len(list(ActionTaken)) == 8`).
   - Print a single `PASS` line or raise/print the observed value on failure.
   - Use the real enum/field names from the diff — run a quick `uv run python -c "from options_agent.contracts import X; print(list(X))"` first if you need to confirm exact values before asserting.

3. **Collect results** — record each check as `PASS` or `FAIL (observed: <value>, expected: <value>)`.

**If any check FAILS:** treat it as a concrete bug under `### Bugs and logical inconsistencies`, with the actual vs. expected values. A failing author verification overrides passing static analysis — the code is not correct if the author's own stated claims don't hold.

**If no `Verify` lines are found in the PR body:** write "No author verification steps found." in the Verification results section and proceed.

### Phase 3 — Deliver the review

Output a structured review using exactly this template:

```
## Review: [WP-N.M] <name>
**PR:** <link>  |  **Files changed:** N  |  **+<A> / -<D> lines**
**Selected because:** <one sentence from Phase 1 ranking rationale>

---

### Verification results
<Results of running author-stated verification steps from the PR body. One line per check: description → PASS / FAIL (observed: ..., expected: ...). If no Verify steps were found in the PR body, write "No author verification steps found.">

### Implementation analysis
<Bullet-point findings. Cover all of the following dimensions — omit a dimension only if there is genuinely nothing to say:>
- **Acceptance criteria coverage** — list each criterion from the card; mark ✓ met or ✗ not met, with a file:line reference for ✗ items
- **Contract alignment** — check Produces / types / schemas against the contracts in options-agent-plan.md; flag mismatches
- **Edge cases and error paths** — unhandled inputs, missing guards, silent failures
- **Test coverage** — missing cases, wrong assertions, tests that pass vacuously

### Cross-WP clarifications needed
<Decisions the implementer and other WP owners must align on before this merges. For each item, name the affected downstream WP. If none, write "None identified.">

### Bugs and logical inconsistencies
<Concrete defects — file:line reference + one-line description each. All references must come from the actual diff output, never hallucinated. If none, write "None found.">

### Verdict
**[Approve | Request changes | Needs discussion]** — one sentence.
```

## Rules

- **Never take merge or approval actions** — the Verdict is a recommendation only; do not call any GitHub or Trello API to approve or merge.
- **All file:line references must come from `gh pr diff` output** — never guess or invent locations.
- **Do not read files outside the PR diff** unless a cross-reference in the diff points to a contract file in `docs/` that is needed to verify correctness.
- **If a card's `Depends on` entry is not "Done"**, flag it explicitly in Cross-WP clarifications as an integration risk — do not assume the interface is stable.
- **Stay within the card's scope** — do not critique design decisions that were explicitly documented as resolved in the card description or the PR body's "Decisions resolved" table.
