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

   **If `<WP-N>` is `WP-9`:** also read the "WP-9 (Ops Console UI) — carried-forward context" section near the end of this file before Phase 3 — it has exact design tokens, the design reference's file-access gotcha, and the docker visual-verification workflow, all learned the hard way in WP-9.2. Re-deriving any of it from scratch is wasted effort.

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

1. **Create a dedicated worktree for this ticket** — call `EnterWorktree` with `name: wp-N.M-<short-slug>` (e.g. `wp-0.8-repo-skeleton-ci`). This creates an isolated git worktree under `.claude/worktrees/` on a new branch based on `origin/<default-branch>` (the tool's default `worktree.baseRef: fresh` behavior), and switches the session into it. This sidesteps the staleness trap entirely — don't trust the session's initial `gitStatus` snapshot or whatever branch happens to be checked out in the main working tree; a prior session can leave it on a stale, already-merged branch, or local `main` can lag a PR that merged since the last sync. Branching from stale state produces a Phase 3 briefing with wrong assumptions — e.g. WP-9.5 initially assumed "no frontend test framework exists" (true through WP-9.4) because local `main` hadn't picked up a test-harness PR that had just merged. `EnterWorktree` branching fresh from `origin` avoids that failure mode by construction. All subsequent implementation, testing, and PR work for this ticket happens inside the worktree.

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

After CI passes, update `docs/` to reflect what landed. **Docs are high-level by policy** (established by the 2026-07-19 documentation audit, which stripped ~40% of accumulated per-ticket detail — do not reintroduce it). `docs/features/*.md` describe what a sub-system does, its module map, and the design invariants a maintainer must not break. They are not changelogs, PR summaries, or API references.

Rules:

- **No WP/card bookkeeping in docs.** No WP numbers in headers, section titles, or status lines (write `Status: complete`, or `in progress (<what remains>)` — never `complete (WP-9.3)`). Ticket attribution lives in Trello and git history.
- **No per-decision narratives.** "Why X, not Y (decision, date)" paragraphs belong in the PR body's "Decisions resolved" table. Promote a decision into the doc only if it is an invariant future code must preserve — then state the invariant, not the deliberation.
- **No long runnable walkthroughs.** Don't paste multi-line object-construction examples; point to the canonical test file instead ("see `tests/test_crud.py`"). A snippet over ~10 lines must earn its place.
- **One home per concept — link, don't duplicate.** Kill-switch states → `runbook_kill_switch.md`; entry pipeline → `features/orchestrator.md`; monitor exit rules → `features/monitor.md`; IV warm-up → `features/data-signals.md`; setup/env vars → `README.md`. Check for an existing home before writing a new section.
- **Update in place, never append.** If a change makes existing doc text wrong or stale, correcting that text is part of the ticket — no dated addendum sections below stale prose.
- **New sub-system:** create `docs/features/<name>.md` in the established style (header block: module, credentials, status; sub-modules table; invariants) and add a row to `README.md`'s sub-system table.
- **WP completed by this ticket:** update the WP's Status line and tick its DoD boxes in `docs/WORKSTREAMS.md` in the same PR.

Include all doc changes in the same commit in Phase 6 (or an immediately following commit on the same branch before the PR is opened).

### Phase 5.75 — Stand up the console demo (WP-9 only)

For any `WP-9.x` ticket, once CI passes (Phase 5) and docs are updated (Phase 5.5), bring up the demo container automatically — don't wait for the user to ask, and don't skip it because the change "seems backend-only." Follow the "Visual verification workflow (docker)" steps in the WP-9 carried-forward context below: rebuild the image, seed (or reuse) a scratch DB, run `console-demo` on port 8001. This gives the user a running instance to review against the design reference while the PR is open, and is also the point to sanity-check the change yourself against real seeded data (e.g. exercise a new endpoint with `curl` before claiming it works). Leave the container running afterward (per that workflow's step 5) rather than tearing it down immediately — the user may want to poke at it themselves.

**Worktree gotcha:** `docker compose build` derives its image tag and project name from the current directory's basename when no `name:` is set in `docker-compose.yml` (true here — checked, none is set). Building from inside a `wp-N.M-<slug>` worktree therefore tags the image `wp-n-m-<slug>-console` instead of `agent_options_trading-console`, which breaks the hardcoded `agent_options_trading-console:latest` reference in the `docker run` command below. Pin the project name explicitly when building from a worktree: `docker compose -p agent_options_trading build console` (or `COMPOSE_PROJECT_NAME=agent_options_trading docker compose build console`).

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

### Phase 8 — Exit the worktree

Call `ExitWorktree` with `action: "keep"` — leave the worktree and its branch on disk. Do not remove it yet: follow-up commits often land on this branch after Phase 7 (per the Rules section below), and the WP-9 demo container (Phase 5.75), if still running, is serving files from inside it. This returns the session's working directory to where it was before Phase 4, but the worktree remains available to re-enter (`EnterWorktree` with the same `path`) for any follow-up work on this ticket.

**Final cleanup:** once the user has verified there are no further changes to be made for this ticket (e.g. review feedback is addressed, CI is green, and they explicitly say they're done, or the PR has merged), tear down the WP-9 demo container if one is still running (`docker rm -f console-demo`), then re-enter the worktree if the session isn't already in it and call `ExitWorktree` with `action: "remove"` to delete the worktree and its branch. Don't remove it proactively before that confirmation — a still-open PR commonly gets one more round of fixes.

## WP-9 (Ops Console UI) — carried-forward context

Everything below was learned building WP-9.2 through WP-9.5 (Overview, Decisions, live activity, Performance & bias) and applies to every remaining WP-9.x screen ticket (Ask, and any WP-9.6+ card layered on an existing screen). Read this section whenever `<WP-N>` is `WP-9`.

**Frontend test framework exists as of a test-harness PR merged mid-WP-9.5** (Vitest + React Testing Library + MSW mock server + Playwright e2e smoke, with matching `lint`/`build`/unit/`test:e2e` CI jobs). Don't assume otherwise the way WP-9.5's initial Phase 3 briefing did (see the git-staleness note in Phase 4 step 1 above — that assumption was already wrong by the time it was written). Any new WP-9.x screen component should ship with `*.test.tsx` coverage under `frontend/src/components/` plus MSW fixtures/handlers in `frontend/src/test/msw/handlers.ts`, matching the pattern established for the Overview/Decisions/Performance screens.

**Backend API wrapper conventions (established WP-9.5, applies to any future `/api/review/*` or similar wrapper):**
- Fetch shape must mirror `obs/__main__.py`'s CLI fetch exactly (`query_journal(date_from=since)` → `position_ids` from `OPENED`/`CLOSED`/`ROLLED` records → `query_outcome_records(position_ids=...)`) — this is what makes an endpoint's numbers parity-testable against `python -m options_agent.obs review`/`bias`, a WP-9 epic definition-of-done requirement. Don't invent a different fetch path.
- `math.nan` in a dataclass report field must become `None` at the API boundary (`ui/review.py`'s `_nn()` helper) — raw `json.dumps` emits an invalid `NaN` token that `JSON.parse` rejects in the browser. Never let a pure `obs/` function's NaN reach a response model untouched.
- "Insufficient (n < floor)" display gating belongs in the `ui/` wrapper layer, not the `obs/` pure function — e.g. `hit_rate_by_strategy()` has no sample-size floor of its own; `ui/review.py` applies `Limits.bias_min_sample_size` on top of it as a presentation concern. Keep pure functions untouched; adding a floor to one directly would be a "new metric," typically out of a card's stated scope.
- `GET /api/review/prompt-versions` already exists (WP-9.5, `ui/review.py:get_prompt_versions`) — distinct `prompt_version` values across the full journal, ignoring any `since` filter. WP-9.6's compare view (or anything else needing a version picker) should reuse this, not add a second endpoint.

**Design reference:** `https://claude.ai/code/artifact/ba602f8d-fd08-4c36-8fc5-93fa8a3efd3a` is the canonical visual/behavioral spec for all four screens, not just Overview. Each screen's markup lives in the same document under section anchors `#overview` (done), `#decisions`, `#performance`, `#ask` — grep for the anchor instead of reading the whole ~60KB doc.

**Fetching it — avoid re-deriving colors from a screenshot:**
- `WebFetch` on this URL saves the **full HTML to a local file** even though the tool's returned text preview truncates partway through (typically mid-`<style>`-block). The saved path is printed in the tool result header (`...tool-results/artifact-<id>-<timestamp>.html`) — `Read` that file directly (or `grep` it) instead of assuming the truncated preview is all there is, and instead of asking the user to fetch it for you.
- **Critical values live in inline `<script>` tags, not the `<style>` block.** The equity-curve chart (and any other screen with a hand-drawn SVG — funnel bars, skew meter, attribution bars) is built by vanilla-JS at the bottom of the document, with its own hardcoded colors/opacities that never appear in the CSS. Grep the saved file for the section's chart `id` (e.g. `eqchart`) to find its drawing script before assuming a CSS custom property covers it.

**Exact design tokens** (confirmed from the reference's `<style>` block — reuse verbatim, don't re-derive from a screenshot):
```
--ground:#0F141B   --surface:#151C26   --raised:#1B2431   --inset:#111823
--line:#26303F     --line-soft:#1E2836
--ink:#E9EEF5      --ink2:#9AA8BA      --ink3:#5F6E82
--accent:#3987e5   --accent-soft:#7FB4E5
--good:#0ca30c     --warn:#fab219      --crit:#d03b3b
--gain:#1fb14b     --loss:#e05e5e
--f1:#6da7ec --f2:#5598e7 --f3:#3987e5 --f4:#256abf --f5:#1c5cab   (funnel bar shades, Performance screen)
mono: ui-monospace,"SF Mono","Cascadia Code",Consolas,"Liberation Mono",monospace
sans: system-ui,-apple-system,"Segoe UI",sans-serif
```
Equity-curve-chart-specific (inline-JS only, confirmed WP-9.2): gridline stroke `#232E3C` (not a named token — sits between `--line` and `--line-soft`), area fill `rgba(57,135,229,0.10)`, hover crosshair stroke `#3E4C5F` dashed, hover dot fill `--accent-soft`. Draw gridlines/area before the line/dot layer if you want the semi-transparent area fill to not wash them out (the reference itself doesn't bother — its gridlines render slightly muted under the fill — but WP-9.2 intentionally reordered for legibility per user feedback; use judgment per chart).

**Structure:** `.frame` (outer border + radius 12px + `box-shadow:0 24px 60px -32px rgba(0,0,0,.7)`) wraps an `.appbar` (header, `background:var(--raised)`, 48px tall) and a `.screen` (padded content, `background:var(--surface)`, 18px padding, 16px gap). Numeric table columns use a `.num` class (both `<th>` and `<td>`) for right-alignment + tabular-nums; everything else stays left-aligned.

**Header tabs are presentational, not routed — but screens can still be reachable.** The reference's `.apptabs` are plain `<span>` elements (`Overview Decisions Performance Ask`), not `<a>` links, and there's still no client-side router in this codebase (not yet scheduled — likely lands with WP-9.9, whose citations must deep-link into the Decision explorer). As of WP-9.3, tabs for *built* screens are wired to local React state in `App.tsx` (`screen`, plus `selectedCycleId` for cross-screen deep-linking later) that swaps which screen renders — no URL change, no router. Tabs for screens that don't exist yet stay inert `<span>`s. Keep both state variables centralized in `App.tsx` so the eventual router swap is contained.

**Visual verification workflow (docker):**
1. `docker compose build console && docker compose up -d console` — rebuilds and restarts the *real* service. Confirms no crashes, but `agent_data` is the live trading DB and is usually sparse/empty — not useful for a populated screenshot.
2. For a populated visual, seed a **scratch SQLite DB** — never write fixture rows into the real `agent_data` volume. Use the checked-in, reusable fixture script rather than hand-rolling one per ticket:
   ```bash
   uv run python scripts/seed_console_demo_data.py <scratch-dir>/dev.db --force
   ```
   It migrates the target path via `alembic upgrade head` and seeds journal cycles (one of every `ActionTaken`, including a deliberately-broken position link to exercise anomaly rendering), open positions spanning the distance-to-trigger spread, and closed-position history for the equity curve — anchored to "now" so `Cycles Today` is always populated regardless of when it's run. Add more seed data to that script rather than a scratch one-off if a future screen needs a shape it doesn't cover yet.

   Then run a **separate, non-compose container** against it:
   ```bash
   docker run -d --rm --name console-demo -p 127.0.0.1:8001:8000 \
     -v <scratch-dir>:/app/demo-data \
     -e DB_URL=sqlite:////app/demo-data/dev.db \
     agent_options_trading-console:latest
   ```
   `-e DB_URL=...` alone is sufficient for the container — no config.toml mount needed (`ui.app.create_app()` reads `DB_URL` as an override regardless of whether a config.toml was found; verified WP-9.5, see `scripts/seed_console_demo_data.py`'s docstring). A curl immediately after `docker run -d` can transiently fail (connection reset, curl exit 56) while uvicorn finishes starting — wait a couple seconds and retry rather than assuming the container is broken.

   **Testing anything under `/api/ask` needs `ANTHROPIC_API_KEY` too** — add `-e ANTHROPIC_API_KEY="$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)"` to the `docker run` command above. Use command substitution exactly like that (not a literal `-e ANTHROPIC_API_KEY=sk-...` with the value typed inline) — the raw key never appears in the command string the Bash tool logs, only its value flows into the container's env at execution time. **Never run anything that echoes/prints `.env` contents to verify this worked** (e.g. `docker compose config` with no redaction, `cat .env`, `env` inside the container). If you need to confirm the container has the var, check for a successful `/api/ask` response instead (an auth failure surfaces as a 401/500 from the Anthropic SDK), or pipe any inspection command through a redaction filter first.

   **Cross-checking against the CLI is a separate trap.** `python -m options_agent.obs review`/`bias`'s `_load_config()` prefers a checked-in `config.toml` over `DB_URL` unconditionally — running it from the repo root (which has one, pointing at the real dev DB) silently ignores your `DB_URL` override and reports numbers from the wrong database, with no error. Run it from a cwd with no `config.toml` instead (e.g. `cd <scratch-dir> && DB_URL=... uv --project <repo-root> run python -m options_agent.obs review`).
3. **`docker restart` does not pick up a rebuilt image** — a container is pinned to the image snapshot from its `docker run`. After every rebuild (or reseed — a replaced DB file can leave stale connections in an already-running container), `docker rm -f console-demo` then re-`docker run` from the fresh tag/DB. `docker stop` with `--rm` can race the name becoming free again — prefer `docker rm -f` immediately before recreating.
4. **A real browser is available via the Playwright MCP server** (`mcp__playwright__browser_navigate`, `browser_snapshot`, `browser_click`, etc.) — check `ToolSearch` for `mcp__playwright__*` tools before assuming otherwise; this was a confirmed dead end through WP-9.2/9.3 but was fixed mid-WP-9.3 by installing real Google Chrome as root (`apt` from Google's repo, landing at `/opt/google/chrome/chrome` — the path the MCP server's `channel: "chrome"` config looks for). If the tools are missing or `browser_navigate` errors with a Chrome-not-found message, ask the user to install it (see git history around WP-9.3 for the exact apt commands) rather than falling back to asking them to screenshot manually. `browser_snapshot` (accessibility tree, returned inline) is more reliable for verifying exact text/values than `browser_take_screenshot` — the latter's output file saves to whatever host actually runs the browser, which has **not** been reachable from this sandbox's Bash/Read tools in practice (a separate filesystem from where Claude Code itself runs, even though both happen to have Chrome at the same path). Use snapshots as the primary signal; treat screenshots as a bonus only if the user confirms they can see the saved file themselves. For things a snapshot's text can't show — computed colors, gradient ordering, whether an absolutely-positioned element escaped its container — use `mcp__playwright__browser_evaluate` to read `getComputedStyle(...)` or `getBoundingClientRect()` directly (e.g. confirming a funnel bar's `background` matches the expected token, or that a child element's bounding box stays within its parent panel's). This caught and confirmed a real containment bug in WP-9.5 that a text-only snapshot would have missed entirely.

   The Playwright browser session can close between conversation turns (e.g. after a long gap or a container restart) — if `browser_navigate`/`browser_click` errors with something like "Target page, context or browser has been closed," just re-issue the same `browser_navigate` call; it opens a fresh session and continues normally. Element refs (`e12`, `e34`, ...) from a snapshot only stay valid until the next navigation/snapshot — re-snapshot after any click that changes the page rather than reusing old refs.
5. **When iterating on visual/design feedback across multiple turns, leave the demo container running between rounds** — don't tear it down after each fix. Rebuild the image and `docker rm -f console-demo` + re-`docker run` (step 3) after every code change, but only stop the container for good (`docker rm -f console-demo`) once the user confirms there are no more observations, or explicitly asks you to stop it. Tearing it down proactively after every single round just forces an avoidable rebuild-and-renavigate cycle next turn.

## Rules

- Never start implementation without user confirmation in Phase 3.
- Never open a PR (Phase 6) until all four CI gates pass (Phase 5): ruff check, ruff format, pyright, pytest.
- Never merge a PR without explicit approval.
- Never exceed the card's stated scope — if you discover necessary work outside the card, flag it rather than doing it silently.
- If a dependency card is not "Done", implement against a typed stub and document the assumption in a code comment.
- If the card is in "Needs Reqs" or "Needs Implementation Details", list every open question and wait for answers — do not invent defaults.
- If follow-up commits are pushed to the PR after Phase 7 (e.g. to align with another branch or resolve a conflict), post an updated comment on the Trello card explaining what changed and why. Re-check whether any decisions recorded in the Phase 3 briefing or the existing Trello comment are now stale and correct them.
- Never call `ExitWorktree` with `action: "remove"` until the user has confirmed there's nothing further to change for this ticket — removing it early destroys in-progress follow-up work with no way back.
