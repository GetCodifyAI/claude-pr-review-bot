# Architecture

Read this before changing anything. Most of the layout decisions here look arbitrary and are
not — each one is working around something specific about the Cut+Dry staging box.

## The pieces

| File              | Runs as                   | Does                                                            |
| ----------------- | ------------------------- | --------------------------------------------------------------- |
| `pr-watch.sh`     | cron, every 3 min         | Finds PRs awaiting your review → `queue.json` + Slack card      |
| `prbot-server.py` | systemd, `127.0.0.1:8899` | The dashboard: renders reviews, posts, approves                 |
| `run-review.sh`   | spawned per click         | Worktree → `claude -p` → `review.json`. Never writes to GitHub  |
| `prbot_diff.py`   | imported                  | Diff-anchor validation, so GitHub can't 422 the whole review    |
| `prbot_md.py`     | imported                  | Dependency-free markdown → HTML (headings, tables, code, lists) |
| `lib-common.sh`   | sourced                   | Config, HMAC signing, Slack posting                             |
| `bootstrap.sh`    | you, once                 | Installs all of the above                                       |

## Why everything lives in `~/.claude-pr-bot/`

Because `pnpm staging:sync` runs `rsync --delete` into `/var/local/cut-dry/current/`
(`scripts/sync-to-staging.sh:98` in the monolith). Anything inside that path can be deleted
out from under a running review. So the base clone, the worktrees, the per-PR state and the
secrets all sit outside it, and the Apache vhost lives in `/etc/apache2/sites-available/`
for the same reason.

## Why a dedicated vhost on its own hostname

The app's own vhost (`conf-enabled/cut-dry.conf`) rewrites every unmatched path into
`/index.php/$1`. And VirtualHosts do **not** inherit `ProxyPass` from the global server
config — so a global snippet is silently ignored and `/prbot` 404s straight through PHP.

Editing `cut-dry.conf` would work, but CodeDeploy owns that file and reverts it.

So: a dedicated name-based vhost on its own hostname. The nonprod staging nginx proxy maps
`~^.*<env>\.staging\.eng\.cutanddry\.com$` to your box, and the ALB cert is
`*.staging.eng.cutanddry.com` — so `prbot-<env>.staging.eng.cutanddry.com` reaches you with
TLS, and nothing the app owns is touched. The app stays reachable on `app-<env>` as before.

That hostname is derived from a single `PRBOT_ENV` value rather than configured twice, so the
link in Slack and the vhost Apache is listening on cannot drift apart.

## The dashboard

| Route                    |                                                                  |
| ------------------------ | ---------------------------------------------------------------- |
| `GET /prbot/login` `POST` | Sign in with a GitHub PAT + optional Slack member ID            |
| `GET /prbot/settings` `POST` | Update Slack ID / rotate PAT / sign out                      |
| `GET /prbot/?tab=&sort=` | Index — **your** queue: tabs, sorting, dates                     |
| `GET /prbot/pr?pr=N`     | Detail — timeline, assessment, prose, editable findings, actions |
| `GET /prbot/review?pr=N` | Start a review, redirect to the detail page                      |
| `POST /prbot/post`       | Post the selected (possibly edited) comments                     |
| `POST /prbot/approve`    | LGTM comment + approve                                           |
| `GET /prbot/health`      | Liveness                                                         |

**Tabs** — `To review` · `Reviewed` · `Posted` · `Approved` · `All`, so finished work leaves
the working set. **Sorting** — newest (default), oldest, recent activity, most findings.
Rows carry author, diff size, dates (`MM/DD/YY`) and severity chips.

**Detail** opens with a timeline (`✓ Reviewed · ✓ Comments posted · ○ Approved`), then the
agent's assessment, then collapsible *What this PR does* / *Analysis*, then the findings —
each a card with a checkbox, severity pill, `file:line`, a reply-vs-new badge and an editable
textarea. A sticky bar tracks the selected count.

Once approved, the approve form is replaced by a card showing when it happened and the exact
comment posted with it.

## Design decisions worth defending

- **Posting is always a plain `COMMENT` review.** Never `REQUEST_CHANGES` — these are review
  notes, not a merge block, and the human approves separately. The agent's own verdict is
  surfaced as an *assessment* only.
- **Approve posts a comment and then approves.** The body is pre-filled with `LGTM` plus a
  checklist of the blocker/should-fix findings (nits omitted), and is editable first.
- **`run-review.sh` never touches GitHub.** It only produces `review.json`. Every write is a
  separate, deliberate human click. This is the property that makes the whole thing safe to
  run against a real review queue.
- **Slack dedup is per PR + login, not per head SHA.** Each reviewer is pinged once per PR and
  never again; pushing new commits must not re-ping anyone. The dashboard always reflects the live queue
  regardless of what has been announced, so Slack is a one-time nudge, not the source of
  truth.
- **One search per user per poll, not a page-through.** The monolith sees 200+ PR updates a
  week. `review-requested:<login>` resolves server-side, so the poll costs one API call per
  user per 3 minutes, and `queue.json` means page loads never call `gh` at all.

## Per-PR state

`~/.claude-pr-bot/state/<pr>/`:

| File            | What                                                              |
| --------------- | ------------------------------------------------------------------ |
| `review.json`   | The agent's raw output — the artefact the dashboard renders       |
| `meta.json`     | PR identity, cached so titles survive after the PR leaves the queue |
| `payload.json`  | Exactly what was sent to GitHub                                   |
| `posted.json`   | Marker + timestamp                                                |
| `approved`      | Marker + timestamp                                                |
| `agent.log`     | What the agent did                                                |
| `run.log`       | The run wrapper's log                                             |
| `status`        | `fetching` / `reviewing` / `done (N findings)` / `failed: …`      |

Per-user markers live one level down, in `state/<pr>/users/<login>/`: `opened`, `posted.json`,
`payload.json`, `approved`, `archived`. The review itself stays at `state/<pr>/` and is shared.
Markers found directly in `state/<pr>/` predate multi-user and are read as the box owner's.

`queue.json` rows carry `requested: [logins]` — the union of everyone awaiting that PR — and
the dashboard filters on it, so one poller output serves every user.

`meta.json` exists because `queue.json` only holds PRs *currently* awaiting review — the
moment you submit, the PR drops out of it, and without the cache the dashboard would lose the
title of the review you just ran.

## The review step

`run-review.sh` creates a git worktree off the PR's head, then runs headless Claude Code with
`--allowedTools "Bash Read Glob Grep Write"` and a 25-minute timeout, pointing it at the
`pr-review` skill in **automation mode** (Step 7 of the skill): no bottom table, no GitHub
writes, just a `review.json` with `event`, `summary`, `explainer`, `analysis`, and a
`comments` array carrying `path`, `line`, `severity`, `body` and `reply_to`.

The prompt tells the agent a human will read `explainer` and `analysis` in a dashboard to
decide whether to trust the findings. That framing is load-bearing — it is what makes the
prose readable rather than a wall of bullet points.

The worktree is removed as soon as `review.json` is copied out.

## Subsystems added since the first cut

- **Learnings** (`prbot_learn.py`): on post, each original finding is scored dropped / edited /
  kept and appended to `learnings.jsonl` (short gists, capped). `render()` folds recent
  dropped/edited rows into the next review prompt so the agent stops re-raising rejected noise;
  the `/learnings` page shows it. Shared per repo, attributed per user. Not ML — in-context
  steering with your own recent decisions.
- **Skills**: a user can bring their own review skill (`~/.claude-pr-bot/skills/<login>.md`).
  `run-review.sh` picks the clicker's skill (`PRBOT_ACTOR`) or the global default, runs its
  logic, and **always appends an explicit `review.json` output contract**, so any skill yields
  the shape the dashboard needs. Each review records the skill id (`state/<pr>/skill`); learnings
  rows carry it; the `/skills` page scores each skill by kept-rate. The flywheel: usage →
  accept/reject signal → which skills work → a better global skill (human-approved).
- **Suggestion blocks**: a finding may carry a `suggestion` (single-line replacement); on post
  it's appended to the comment body as a GitHub ```` ```suggestion ```` block (one-click apply).
- **Staleness**: `run-review.sh` records the reviewed head SHA (`state/<pr>/head`); the detail
  page flags the review stale when the PR's current head differs — without auto-re-running.
- **Slack threading**: with `SLACK_BOT_TOKEN` + `SLACK_CHANNEL`, `slack_post` uses
  `chat.postMessage`, stores the request card's ts, and threads the review-ready reply under it;
  otherwise it falls back to the send-only webhook.

## Relationship to the `@patchwork` bot

There is a sibling in the monolith at `tools/claude-pr-bot/pr-bot.sh` — the `@patchwork`
comment bot, which reacts to PR comments and pushes commits. They are deliberately separate:
that one reacts to comments and writes code, this one reacts to review requests and writes
nothing. They share a box and a directory-layout convention, and nothing else.
