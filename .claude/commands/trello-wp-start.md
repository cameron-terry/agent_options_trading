Fetch a specific Trello ticket from the "Agentic Trading System" board and begin implementation, asking for design clarifications first.

## Usage
`/trello-wp-start <WP-N.M>` — e.g. `/trello-wp-start WP-0.8` or `/trello-wp-start WP-1.2`

## What this skill does

### Phase 1 — Fetch and understand the ticket

1. **Find the board** named "Agentic Trading System" using `mcp__trello__list_boards`.

2. **Fetch all cards and lists in parallel** with `mcp__trello__trello_get_board_cards` and `mcp__trello__get_board_details` (includeDetails: true).

3. **Find the target card** whose name starts with `[<WP-N.M>]` (e.g. `[WP-0.8]`). Also find the parent epic card whose name starts with `[WP-N]` (e.g. `[WP-0]`).

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

### Phase 6 — Open a PR

Once all CI gates are green, commit the work and open a pull request:

1. Stage and commit all changes with a message that summarises the WP scope and any non-obvious decisions made during implementation (toolchain choices, schema resolutions, dependency assumptions, etc.).

2. Push the branch and create a PR using `gh pr create`. The PR body should include:
   - **Summary** — bullet points describing what was built
   - **Decisions resolved** — a table of any design/toolchain choices made during this WP (the "why" future readers need), drawn from the Phase 3 briefing and any follow-up discussion
   - **Test plan** — a checklist the reviewer can run manually to verify the acceptance criteria

3. Post the PR URL to the user and wait for them to review and test.

### Phase 7 — Wait for explicit user approval

**Do not touch Trello until the user explicitly approves.** The user must test the changes and review the PR themselves.

When the user signals approval (e.g. "looks good", "ship it", "move the card"), proceed to Phase 8. Any other response — questions, change requests, bug reports — means Phase 4 is still active; address the feedback, re-run CI (Phase 5), update the PR, and wait again.

### Phase 8 — Update Trello

Only after explicit user approval in Phase 7:

1. Post a comment on the Trello card summarising what was done and linking the PR, using `mcp__trello__trello_add_comment`.

2. Move the card to "In Review" using `mcp__trello__move_card`.

## Rules

- Never start implementation without user confirmation in Phase 3.
- Never open a PR (Phase 6) until all four CI gates pass (Phase 5): ruff check, ruff format, pyright, pytest.
- Never update Trello (comment or card move) without explicit user approval in Phase 7.
- Never exceed the card's stated scope — if you discover necessary work outside the card, flag it rather than doing it silently.
- If a dependency card is not "Done", implement against a typed stub and document the assumption in a code comment.
- If the card is in "Needs Reqs" or "Needs Implementation Details", list every open question and wait for answers — do not invent defaults.
