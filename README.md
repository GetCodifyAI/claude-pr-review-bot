# Robin — the Claude PR review bot

**Robin drafts the PR reviews you owe your team, and lets you send them with a click — under
your own name, never automatically.** A reviewer's assistant, not a review bot.

Someone requests your review on a Cut+Dry PR → you get a **Slack ping** → you click through
to a **dashboard** → the agent has already read the diff and written findings → you tick the
ones worth posting, edit any of them, post **as yourself**, and approve — in one place.

Nothing reaches GitHub without a human clicking. The review step never writes to GitHub at
all — it only produces a JSON file the dashboard renders.

## Features

- **Per-reviewer identity** — every comment and approval posts under *your* GitHub account
  (your own token), never a bot. One box serves the whole team; the review is shared per PR,
  posting/approval is per person.
- **Learnings loop** — when you drop a finding as noise or reword one, Robin remembers and
  feeds it into the next review of the repo, so it stops repeating what you reject.
- **Deliberate run** — starting a review is a small form, not a one-click: choose **Quick /
  Standard / Deep** effort (auto-suggested from the diff) and optionally add a **focus note**
  ("pay attention to the order-flow cost calc") that Robin folds in on top of the skill.
- **Re-run with history** — re-run at a different effort or focus any time; the previous run is
  kept in **history** and viewable, never overwritten.
- **Stop** — a running review can be stopped from the progress panel.
- **Choose which skill runs** — the **Skills** page has one selector: run reviews with the
  shared **team default** or **your own skill**. The team default is editable in the browser and
  protected (it can't be blanked, and restoring the built-in takes a typed confirm).
- **Quick-add a rule** — type a preference in plain words ("don't ask for a Jira link in code
  comments") and Robin tidies it into the skill's *Team rules* section — no editing the whole
  file.
- **Bring your own review skill** — paste your `pr-review` skill in Integrations; Robin runs
  its logic and appends its own output contract so any skill works. A **Skills** page scores
  each skill by how often its findings are kept vs dropped — the signal for improving the
  shared default.
- **Domain-risk context** — a review flags when the PR touches pricing, catalog or DP/integration
  paths, so the reviewer looks harder there. Context only — it never routes or auto-posts.
- **GitHub suggestion blocks** — findings can carry a one-click-apply code fix for the author.
- **Re-review on push** — flags a review as stale when the author pushes new commits.
- **Per-user Claude account** — connect your own Claude account so reviews you start bill to
  your plan; otherwise they use the shared box login.
- **Human-gated & COMMENT-only** — nothing auto-posts, and Robin never requests changes or
  blocks a merge.

```
review requested
  └─ pr-watch.sh (cron, every 3 min) → queue.json + Slack card
       └─ you click "Open review"  → /prbot/pr?pr=N   (first visit starts the review)
            └─ run-review.sh → claude -p → review.json          (~10–15 min)
                 └─ Slack: verdict + link back to the dashboard
                      └─ select / edit findings → Post to GitHub  (plain COMMENT review)
                           └─ Approve → LGTM comment + approval
```

## Why this exists

The manual version of this is: notice a review request, pull the branch, run the `pr-review`
skill in Claude Code, read the output, decide what's worth saying, retype it into GitHub.
That is 20 minutes of context-switching per PR, and it is the part that gets skipped when
you're busy.

This automates every step except the two that need judgment: **which findings are worth
posting**, and **whether to approve**. Those stay clicks.

Two things it deliberately does *not* do:

- **It never posts as a bot.** Every comment, review and approval goes out under your own
  GitHub account, via your own PAT. If your name is on it, you chose it.
- **It never requests changes.** Posting is always a plain `COMMENT` review — these are
  review notes, not a merge block. The agent's verdict is shown to you as an *assessment*
  and nothing more.

## Get started

- **[docs/SETUP.md](docs/SETUP.md)** — the install, start to finish. Budget 30 minutes.
- **[docs/OPERATIONS.md](docs/OPERATIONS.md)** — logs, restarts, gotchas, known limits.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how the pieces fit and why they sit
  where they do. Read this before changing anything.
- **[docs/SECURITY.md](docs/SECURITY.md)** — the endpoint is on the public internet. Read
  this before going live.

**One box serves the whole team.** Each person signs in once with their own GitHub token
(+ Slack member ID), and from then on: review requested → Slack pings *them* → they open the
dashboard → the agent's findings are already there → they tick, edit, post and approve —
**as themselves**. The review is shared per PR (one agent run, however many reviewers);
selection, posting and approval are per person. See [docs/SETUP.md](docs/SETUP.md#team-pilot).

## Layout

| Path                        | What                                                                |
| --------------------------- | ------------------------------------------------------------------- |
| `bin/bootstrap.sh`          | Installs everything. Idempotent — re-run it whenever                |
| `bin/pr-watch.sh`           | cron, every 3 min: finds PRs awaiting your review, posts Slack cards |
| `bin/prbot-server.py`       | The dashboard (queue, review, Learnings, Skills, Integrations). systemd, `127.0.0.1:8899` |
| `bin/run-review.sh`         | One review: worktree → `claude -p` (chosen skill + output contract) → `review.json` |
| `bin/prbot_diff.py`         | Diff-anchor validation, so GitHub can't 422 a whole review          |
| `bin/prbot_md.py`           | Dependency-free markdown → HTML                                     |
| `bin/prbot_learn.py`        | Learnings loop: records dropped/edited/kept, scores skills, feeds the review prompt |
| `bin/prbot_assets.py`       | Inlined brand logo + favicon (base64)                              |
| `bin/lib-common.sh`         | Config, HMAC link signing, Slack posting (webhook or bot-token threading) |
| `skills/pr-review/SKILL.md` | The default review procedure. Bootstrap installs it to `~/.claude/skills/` |
| `assets/logo.png`           | Source brand logo                                                   |
| `config.example`            | Every `.env` knob, annotated. Reference only — bootstrap writes the real one |

## Requirements

- A Cut+Dry **nonprod staging box** you control (push a `*-staging` branch, CircleCI builds
  the stack — see `infrastructure/CLAUDE.md` in the monolith).
- **Claude Code signed in** on that box, as the box user.
- A GitHub **classic PAT** with `repo` scope, belonging to you.
- A **Slack incoming webhook** pointed at a private channel. Optional, but the Slack card is
  most of the value — without it you have to remember to open the dashboard.

## A caveat worth knowing up front

Each review is a full agent run against a real diff, on your Claude subscription — roughly
10–15 minutes for a 25-file PR, and it counts against your weekly usage. That is exactly why
the review is **click-to-run** rather than automatic on every review request.
