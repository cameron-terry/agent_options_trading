# Claude Code project configuration

## Skill files

Project-specific slash commands live in `.claude/commands/`. Each `.md` file is a skill the agent can invoke via `/skill-name`:

| Skill | File | Purpose |
|---|---|---|
| `/trello-wp-plan` | `.claude/commands/trello-wp-plan.md` | Create Trello tickets for a WP defined in `docs/WORKSTREAMS.md` |
| `/trello-wp-review` | `.claude/commands/trello-wp-review.md` | Pick the highest-priority "In Review" Trello ticket and deliver a deep code review, including running author-stated verification steps |
| `/trello-wp-start` | `.claude/commands/trello-wp-start.md` | Fetch a Trello ticket and begin implementation |
| `/trello-wp-status` | `.claude/commands/trello-wp-status.md` | Summarize the status of a work package |
| `/trello-wp-evaluate` | `.claude/commands/trello-wp-evaluate.md` | Evaluate completed/in-review WP work for vision drift and unaddressed cross-WP concerns |

## Git workflow

**Always rebase onto main** when updating a feature branch; never merge.

```bash
git fetch origin
git rebase origin/main
```

Resolve conflicts commit-by-commit as the rebase replays. After resolving each conflicted file, `git add <file>` then `git rebase --continue`. Do not use `git merge origin/main` on feature branches.

## Secret safety

`.env` holds real credentials (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ANTHROPIC_API_KEY`, `DISCORD_WEBHOOK_URL`).

- Avoid **`docker compose config`** (and similarly `docker inspect`, `env`, `printenv`, `cat .env`) If you need to sanity-check compose config, redact first:
  ```bash
  docker compose config 2>&1 | sed -E 's/(ALPACA_API_KEY|ALPACA_SECRET_KEY|ANTHROPIC_API_KEY|DISCORD_WEBHOOK_URL): .*/\1: [REDACTED]/'
  ```
  or scope the check narrowly (e.g. `grep -c ANTHROPIC_API_KEY` on a specific service's block to confirm presence, not the value).

- **pytest's default traceback echoes fixture argument values** on a failing test. Always run tier-2 evals with a traceback style that suppresses locals:
  ```bash
  uv run pytest tests/evals/ -m eval --tb=line
  ```

- When a secret's *value* must reach a command (e.g. `docker run -e ANTHROPIC_API_KEY=...`), resolve it via shell command substitution rather than typing the literal value into the command string:
  ```bash
  docker run -e ANTHROPIC_API_KEY="$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)" ...
  ```
  Never write `-e ANTHROPIC_API_KEY=sk-...` with the value spelled out — that string is what gets logged/displayed.

- If a secret leaks into tool output or a transcript despite these precautions, say so immediately and recommend rotation — don't silently continue as if nothing happened.
