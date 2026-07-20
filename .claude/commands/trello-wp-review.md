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

### Phase 2.4 — Check out the PR into a dedicated worktree

Phase 2.5 and Phase 2.6 both execute code from the PR (pytest, manual verification snippets, `docker compose build`, a live UI server). Do that in an isolated worktree, never in the main working tree — the reviewer session shouldn't disturb whatever branch or uncommitted state the user's own checkout is in.

1. **Look for an existing worktree on the PR's branch before creating a new one.** Get the branch name (`gh pr view <number> --json headRefName -q .headRefName`), then check `git worktree list` for a path already checked out to it — most commonly the worktree `/trello-wp-start` created for this same ticket (its Phase 8 keeps that worktree on disk until the user confirms the ticket is fully done, which is typically still true while the card sits in "In Review"). Reusing it means the review runs against the exact working state the implementer left, including anything not yet pushed.
2. **If a matching worktree exists:** call `EnterWorktree` with `path: <that-worktree-path>` to switch into it. Skip `gh pr checkout` — it's already on the right branch. Do not run `git pull` or otherwise mutate it beyond what's needed for read-only verification; it isn't yours.
3. **If no matching worktree exists:** call `EnterWorktree` with `name: review-wp-N.M-<short-slug>` (e.g. `review-wp-9.5-performance-api`) to create a fresh one, then run `gh pr checkout <number>` inside it to fetch and switch to the PR's branch.
4. Run all of Phase 2.5's verification commands and Phase 2.6's build/seed/server steps from inside this worktree.

### Phase 2.5 — Run author verification steps

After Phase 2 completes and you have the PR body, extract and run **all three forms** of author verification:

**Form A — `Verify` checklist lines:** lines matching `- [ ] Verify ...` / `- [x] Verify ...` or plain `Verify <something>`. Write a minimal `uv run python -c "..."` snippet for each, assert the claim, and print `PASS` or the observed value on failure.

**Form B — Shell commands in the Test plan:** any `uv run pytest ...` or `uv run python ...` command in the PR body (typically under a "Test plan" heading). Run each command exactly as written. Record the exit code and key output (pass count, skip count, or error).

**Form C — Python code blocks introduced as manual verification:** any fenced ` ```python ` block preceded by a sentence like "To manually verify", "To verify", "Run in a Python shell", or similar. Execute the block with `uv run python -c "..."` (or write it to a temp file if it spans multiple statements that can't be inlined). Capture stdout/stderr.

**Run all three forms every time.** Do not skip a form because another form was found. If the PR body has a Test plan with pytest commands AND a Python snippet AND a `Verify` line, run all of them.

**Collect results** — record each check as one line:
- Shell command → `PASS (N passed, M skipped)` or `FAIL (exit N — <first error line>)`
- Python snippet → `PASS` (with the observed stdout if informative) or `FAIL (observed: <value>, expected: <value>)`

**If any check FAILS:** treat it as a concrete bug under `### Bugs and logical inconsistencies`, with the actual vs. expected values. A failing author verification overrides passing static analysis — the code is not correct if the author's own stated claims don't hold.

**If the PR body contains none of the above forms:** write "No author verification steps found." in the Verification results section and proceed.

### Phase 2.6 — Visual verification against the design reference (UI cards only)

Skip this phase entirely if the card has no `area:ui` label and touches no files under `frontend/` or `options_agent/ui/`.

1. **Locate the reference artifact.** Search `docs/WORKSTREAMS.md` for a "Design reference:" line in the card's parent WP section (e.g. WP-9 has a `claude.ai/code/artifact/...` mock covering all four console screens) and `docs/features/*.md` for the same link. Fetch it with `WebFetch`, prompting for an exhaustive description of the specific screen this card implements — layout/grid, colors, chip/state semantics, typography, and interaction affordances (expand/collapse indicators, hover states, filter controls). `WebFetch` works on `claude.ai/code/artifact/{uuid}` URLs even though it can't reach most external sites.

2. **Stand up a live instance.** Check `scripts/` for an existing seed/demo-data fixture before writing a one-off. Seed a scratch DB, build the frontend (`cd frontend && npm run build`), copy `frontend/dist/*` into `options_agent/ui/static/` (gitignored — safe to write, never commit), and run `DB_URL=sqlite:///<scratch-db> uv run python -m options_agent.ui --port <port>` in the background.

3. **Drive it with Playwright MCP** (`mcp__playwright__browser_navigate`, `browser_click`, `browser_snapshot`, `browser_take_screenshot`) — load the screen this card implements, exercise its stated interactions (filters, selection, expand/collapse), and use `browser_snapshot` (accessibility tree) as the primary signal for exact text/values; screenshots are a secondary check.

   **If the Playwright browser is already in use** (error: `"Browser is already in use for ... use --isolated"` — a concurrent session holds the shared profile lock): do not kill that Chrome process, it may belong to another active session. Fall back to a static comparison: diff the reference artifact's inline `<style>`/HTML for the relevant screen (already fetched via WebFetch) against the PR diff's component and CSS code. State plainly in the review output that this was a static fallback, not a live render.

4. **Compare against the reference**, dimension by dimension: layout structure (grid columns, pane widths), color/chip semantics (which states map to which color), interaction affordances the mock implies (expand/collapse indicators, hover cursors, disabled states), and any field the API response includes that the mock displays but the component silently drops. A deviation that's documented — in the PR's "Decisions resolved" table or a code comment explaining a real constraint (e.g. "no schema exists to summarize this") — is not a bug. An *undocumented* fidelity gap, especially one that drops already-fetched data or removes a usability affordance the mock relied on, is a legitimate finding for this review.

5. **Tear down** — kill the background server. Deleting the scratch DB and build output is unnecessary if working in the Phase 2.4 worktree — `ExitWorktree`'s Phase 4 removal deletes the whole checkout — but still kill the server process explicitly; worktree removal doesn't touch running processes.

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
- **Doc hygiene** — if the diff touches `docs/`, check it against `/trello-wp-start`'s Phase 5.5 conventions: high-level only, no WP-number bookkeeping in headers/status lines, no per-decision narratives (those belong in the PR body), no >10-line runnable walkthroughs, no duplication of a concept that already has a home doc, and stale text corrected in place rather than appended to. If the diff changes behaviour a feature doc describes but doesn't update that doc, flag it

### Design fidelity vs. reference artifact
<Only for cards touched by Phase 2.6. State whether the comparison was live (Playwright) or static (fallback), then list findings: layout/color/interaction deviations from the reference mock, distinguishing documented (fine) from undocumented (a finding) gaps. If Phase 2.6 was skipped because this isn't a UI card, write "N/A — not a UI card.">

### Cross-WP clarifications needed
<Decisions the implementer and other WP owners must align on before this merges. For each item, name the affected downstream WP. If none, write "None identified.">

### Bugs and logical inconsistencies
<Concrete defects — file:line reference + one-line description each. All references must come from the actual diff output, never hallucinated. If none, write "None found.">

### Verdict
**[Approve | Request changes | Needs discussion]** — one sentence.
```

### Phase 4 — Clean up the worktree

Once the review has been delivered, exit the worktree:

- **If Phase 2.4 created a fresh worktree (step 3):** call `ExitWorktree` with `action: "remove"` (pass `discard_changes: true` if it's refused for uncommitted/unmerged state — nothing in a review-only worktree needs to survive past this point). A code review has no reason to leave a checkout behind; unlike implementation work, there's no follow-up commit expected from this session.
- **If Phase 2.4 reused an existing worktree (step 2):** call `ExitWorktree` with `action: "keep"` — it isn't this session's worktree to delete, and `/trello-wp-start`'s own Phase 8 owns its lifecycle. (`ExitWorktree` refuses to remove a worktree entered via `path` anyway, so `"remove"` would be a no-op here, but state the reasoning in the review output rather than relying on that.)

## Rules

- **Never take merge or approval actions** — the Verdict is a recommendation only; do not call any GitHub or Trello API to approve or merge.
- **All file:line references must come from `gh pr diff` output** — never guess or invent locations.
- **Do not read files outside the PR diff** unless a cross-reference in the diff points to a contract file in `docs/` that is needed to verify correctness.
- **If a card's `Depends on` entry is not "Done"**, flag it explicitly in Cross-WP clarifications as an integration risk — do not assume the interface is stable.
- **Stay within the card's scope** — do not critique design decisions that were explicitly documented as resolved in the card description or the PR body's "Decisions resolved" table.
- **Run all verification and demo work (Phase 2.5, Phase 2.6) inside the Phase 2.4 worktree, never in the main working tree** — always remove it at the end (Phase 4).
