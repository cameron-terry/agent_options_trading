Pull up a work package from the "Agentic Trading System" Trello board and identify its most critical tickets.

## Usage
`/trello-wp-status <WP-N>` — e.g. `/trello-wp-status WP-0` or `/trello-wp-status WP-1`

If no argument is given, default to WP-0.

## What this skill does

1. **Finds the board** named "Agentic Trading System" using `mcp__trello__list_boards`.

2. **Fetches all open cards** with `mcp__trello__trello_get_board_cards` and the board's lists with `mcp__trello__get_board_details` (includeDetails: true). Run these in parallel.

3. **Filters** to cards whose names start with `[<WP-N>` (e.g. `[WP-0` for WP-0), including the epic card (`[WP-0]`) and all sub-tasks (`[WP-0.1]`, `[WP-0.2]`, etc.).

4. **Groups by list** — maps each card's `listId` to its list name. The standard lists are:
   - **To-Do** — spec complete, can start coding
   - **Needs Implementation Details** — scope clear, design/field-level decisions required before coding
   - **Needs Reqs** — missing requirements (numeric defaults, toolchain choices, stakeholder input)
   - **In Review** — PR open or under review
   - **Done** — complete

5. **Analyzes critical path** using the dependency chains documented in each card's description (`**Depends on:**` field). Identify:
   - Which tickets are fully unblocked (no unresolved dependencies, in To-Do)
   - Which tickets are blockers (their completion unblocks the most downstream tickets)
   - Which decisions (Needs Reqs / Needs Impl Details) cascade to the most cards if resolved

6. **Outputs a priority table** ranked by criticality:

   | # | Ticket | List | Why it's critical |
   |---|--------|------|-------------------|
   | 1 | [WP-N.X] name | To-Do | unblocked, root dependency — start now |
   | … | … | … | … |

   Follow the table with:
   - **Immediate actions:** which tickets can be started today, and what decisions must be made first for the blocked ones
   - **Dependency chain summary:** what gets unlocked once the top 2–3 blockers are resolved

## Criticality ranking rules

Apply in order — the first rule that applies wins:

1. **Fully unblocked + root dependency** (many others depend on it, nothing depends on it) → rank highest
2. **Fully unblocked + no dependents** (can be parallelized) → rank second
3. **Blocked, but resolving it unblocks the most downstream tickets** → rank third (note what decision is needed)
4. **Blocked, resolves a single chain** → rank lower
5. **Final gate (depends on everything else)** → rank last

## Output format

- Lead with the board/WP name and a one-line status (e.g. "9 sub-tasks, 0 done")
- Show the priority table
- Close with "Immediate actions" (2–4 bullet points, actionable)
- Keep the whole response under 50 lines — no prose summaries of card descriptions
