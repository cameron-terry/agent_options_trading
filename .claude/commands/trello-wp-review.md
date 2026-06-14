Get all Trello tickets in "In Review" on the "Agentic Trading System" board, select the highest-priority one based on critical-path analysis, and deliver a deep code review.

## Usage
`/trello-wp-review [WP-N.M]` — pass a WP number to target a specific card, or omit to auto-select the highest-priority "In Review" card.

## What this skill does

### Phase 1 — Identify the review candidate

**Known IDs (hardcoded — stable unless the list is deleted and recreated):**
- Board: `6a24e3323bff555727f457b2` ("Agentic Trading System")
- "In Review" list: `6a24e35b9270834ff13b6cff`

**Step 1.** Call `mcp__trello__trello_get_list_cards` directly with the hardcoded "In Review" list ID.

**Fallback (only if step 1 returns an error or an empty/unexpected result):** rediscover IDs by calling `mcp__trello__list_boards` → `mcp__trello__get_lists`, update the hardcoded IDs in this file, then retry `get_list_cards`.

**If a WP argument was supplied (e.g. `WP-0.5.1`):**
- Search the "In Review" results for a card whose name contains that WP number.
- If **not found**: stop immediately and report: `"[WP-N.M] is not in 'In Review' — cannot review. Move the card to 'In Review' and re-run."` Do not proceed to Phase 2.
- If **found**: use that card. Skip scoring. State "Targeted by argument." as the selection rationale.

**If no argument was supplied:**
- If **0 cards** in "In Review": report "Nothing in review right now." and stop.
- If **exactly 1 card**: skip scoring and go directly to Phase 2.
- If **2+ cards**: score by critical-path impact (see below), then go to Phase 2 with the top-ranked card.

**Scoring (only when 2+ cards and no argument):** Call `mcp__trello__trello_get_board_cards` to read all cards and build the full dependency graph from each card's `**Depends on:**` field. Rank using these rules in order — first rule that applies wins:

| Rank | Condition |
|------|-----------|
| 1 | Merging this card unblocks the most To-Do / Needs Impl Details cards downstream |
| 2 | Merging this card unblocks at least one other card and has no unresolved upstream dependencies |
| 3 | Merging this card resolves a single dependency chain |
| 4 | No other cards depend on this card (standalone) |
| 5 | Final gate — depends on most other cards |

Ties → prefer the lower WP sub-task number (e.g. WP-0.3 before WP-0.7).

Output a one-line selection rationale before Phase 2.

### Phase 2 — Gather review context

The card's name and description are already available from Phase 1's `get_list_cards` result — no separate card fetch is needed. Run all of the following in parallel:

1. **Read checklist items** — call `mcp__trello__trello_get_card_checklists` if the card has checklist items. Note the Produces, Acceptance criteria, and Depends-on entries from the description.

2. **Find the PR link** — check card comments via `mcp__trello__trello_get_card_actions` (filter `commentCard`). If no PR link found, check attachments via `mcp__trello__trello_get_card_attachments`. If still not found, report the selected card and ask the user to supply the PR URL, then stop.

3. **Read project docs** — `docs/WORKSTREAMS.md` and `docs/options-agent-plan.md`, focusing on the section for this card's parent WP (e.g. "WP-0" section for WP-0.3).

4. **Fetch the PR** using `gh pr view <number> --json title,body,state,files,additions,deletions` and `gh pr diff <number>`. Read only files touched by the diff — do not scan the whole repo.

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
**Selected because:** <one sentence from Phase 1 selection rationale>

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
