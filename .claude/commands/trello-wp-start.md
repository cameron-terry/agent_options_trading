Fetch a specific Trello ticket from the "Agentic Trading System" board and begin implementation, asking for design clarifications first.

## Usage
`/trello-wp-start <WP-N.M>` — e.g. `/trello-wp-start WP-0.8` or `/trello-wp-start WP-1.2`

## What this skill does

### Phase 1 — Fetch and understand the ticket

**Known IDs (hardcoded — stable unless deleted and recreated):**
- Board: `6a24e3323bff555727f457b2` ("Agentic Trading System")
- "In Review" list: `6a24e35b9270834ff13b6cff`

Do not call `list_boards` or `get_board_details`.

1. **Run in parallel** using the hardcoded board ID:
   - `mcp__trello__trello_search` — search for `<WP-N.M>` (e.g. `WP-0.8`) scoped to the board to find the target card. Do **not** include brackets in the query — the search API does not require them and the card names will contain the tag.
   - `mcp__trello__trello_search` — search for the parent epic `<WP-N>` (e.g. `WP-0`) scoped to the board
   - `mcp__trello__get_lists` — get list IDs and names (needed to display which list the card is in and to move it)

   **Fallback:** if `trello_search` does not return the full card description, call `mcp__trello__get_card` for the specific card ID to retrieve it.

2. **Find the target card** from the search results — the card whose name starts with `[<WP-N.M>]`. Also identify the parent epic card `[WP-N]`.

4. **Read the project docs** — `docs/WORKSTREAMS.md` and `docs/options-agent-plan.md` — to understand the WP's scope, contracts, and design intent. Focus on the section relevant to the parent WP (e.g. "WP-0" section for WP-0.8).

5. **Move the card to "In Progress"** if it is currently in "To-Do". Do NOT move cards that are in "Needs Implementation Details" or "Needs Reqs" — those need questions answered first.

### Phase 2 — Identify open questions

Before writing any code, check for three categories of blockers:

**A. Card-level blockers** — read the card's description for any "Needs Implementation Details" or "Needs Reqs" section. Surface every open question verbatim.

**B. Design decisions not yet resolved** — compare what the card says it Produces against the spec in `options-agent-plan.md`. Flag any gaps: missing field types, unspecified error handling, ambiguous behavior at boundaries.

**C. Dependency gaps** — check each `**Depends on:**` entry in the card description. Are the upstream cards done (in "Done" list)? If not, note what stubs or assumptions you'll need.

### Phase 3 — Ask before coding

Present findings to the user as a structured briefing:

```
## Ticket: [WP-N.M] <name>
**List:** <current list>
**Parent WP:** <epic name>

### What I'll build
<2–3 sentences describing the deliverable based on Produces + Acceptance criteria>

### Open questions  ← only if any exist
1. <specific decision needed, with the two or three concrete options>
2. ...

### Assumptions I'll make if you don't redirect me
- <default choice for each open question, with a one-line rationale>

### Dependency status
- [WP-X.Y] <name> — Done ✓ / In Progress (will stub) / Blocked (explain)

Ready to start — confirm or answer the questions above.
```

If the card is in "Needs Implementation Details" or "Needs Reqs", **do not assume defaults** — stop and require the user to answer all open questions before proceeding.

### Phase 4 — Implement

Only begin after the user confirms (or after answering any open questions):

1. Create a git branch named `wp-N.M-<short-slug>` (e.g. `wp-0.8-repo-skeleton-ci`).

2. Implement the ticket scope as described in the card's Acceptance criteria, guided by the design doc. Stay within scope — do not touch adjacent WPs.

3. Write tests as required by the Acceptance criteria.

4. Notify and iterate with user as necessary until user is satisfied. Do not ask if the user is satisfied, they will notify.

### Phase 5 — Validate CI locally

Before opening a PR, reproduce the full CI pipeline locally in this exact order:

```bash
uv run ruff check .          # lint
uv run ruff format --check . # format
uv run pyright               # type check
uv run pytest                # tests
```

**All four gates must be green.** If any fail:
- Fix the failures (do not skip or suppress checks).
- Re-run the full sequence from the top until all four pass.
- Do not proceed to Phase 6 while any gate is red.

### Phase 5.5 — Update feature docs

After CI passes, update `docs/features/` to reflect what landed in this WP:

- **Extending an existing sub-system** (new functions or behaviour added to `data/`, `state/`, `risk/`, etc.): update the matching `docs/features/*.md` — add or revise the relevant section, update the status line and sub-modules table if needed.
- **New sub-system** (new top-level module or directory with no existing feature doc): create `docs/features/<name>.md` following the style of the other feature docs:
  - Header block: Module path, credentials required, status (WP number)
  - Sub-modules table
  - Usage examples (runnable Python snippets or CLI commands)
  - Any important invariants or failure modes
- **New `docs/features/*.md` created**: add a row to the sub-systems table in `README.md`.

Include all doc file changes in the same commit in Phase 6 (or as an immediately following commit on the same branch before the PR is opened).

### Phase 6 — Open a PR

Once all CI gates are green, commit the work and open a pull request:

1. Stage and commit all changes with a message that summarises the WP scope and any non-obvious decisions made during implementation (toolchain choices, schema resolutions, dependency assumptions, etc.).

2. Push the branch and create a PR using `gh pr create`. The PR body should include:
   - **Summary** — bullet points describing what was built
   - **Decisions resolved** — a table of any design/toolchain choices made during this WP (the "why" future readers need), drawn from the Phase 3 briefing and any follow-up discussion
   - **Test plan** — a checklist the reviewer can run manually to verify the acceptance criteria, should require the reviwer to write + run code if possible

3. Post the PR URL to the user.

### Phase 7 — Update Trello

1. Post a comment on the Trello card summarising what was done and linking the PR, using `mcp__trello__trello_add_comment`.

2. Move the card to "In Review" using `mcp__trello__move_card`.

3. Update the corresponding WP-epic Trello ticket to reflect the ticket's status.

## Rules

- Never start implementation without user confirmation in Phase 3.
- Never open a PR (Phase 6) until all four CI gates pass (Phase 5): ruff check, ruff format, pyright, pytest.
- Never merge a PR without explicit approval.
- Never exceed the card's stated scope — if you discover necessary work outside the card, flag it rather than doing it silently.
- If a dependency card is not "Done", implement against a typed stub and document the assumption in a code comment.
- If the card is in "Needs Reqs" or "Needs Implementation Details", list every open question and wait for answers — do not invent defaults.
- If follow-up commits are pushed to the PR after Phase 7 (e.g. to align with another branch or resolve a conflict), post an updated comment on the Trello card explaining what changed and why. Re-check whether any decisions recorded in the Phase 3 briefing or the existing Trello comment are now stale and correct them.
