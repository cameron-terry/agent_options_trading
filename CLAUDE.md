# Claude Code project configuration

## Skill files

Project-specific slash commands live in `.claude/commands/`. Each `.md` file is a skill the agent can invoke via `/skill-name`:

| Skill | File | Purpose |
|---|---|---|
| `/trello-wp` | `.claude/commands/trello-wp.md` | Create Trello tickets for a WP defined in `docs/WORKSTREAMS.md` |
| `/trello-wp-review` | `.claude/commands/trello-wp-review.md` | Pick the highest-priority "In Review" Trello ticket and deliver a deep code review, including running author-stated verification steps |
| `/trello-wp-start` | `.claude/commands/trello-wp-start.md` | Fetch a Trello ticket and begin implementation |
| `/trello-wp-status` | `.claude/commands/trello-wp-status.md` | Summarize the status of a work package |

## Git workflow

**Always rebase onto main** when updating a feature branch; never merge.

```bash
git fetch origin
git rebase origin/main
```

Resolve conflicts commit-by-commit as the rebase replays. After resolving each conflicted file, `git add <file>` then `git rebase --continue`. Do not use `git merge origin/main` on feature branches.
