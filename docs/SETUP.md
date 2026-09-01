# Setup

Budget 30 minutes, most of it waiting on CircleCI. You end with a Slack card arriving for
every PR that asks for your review, and a dashboard that cannot yet write to GitHub —
`DRY_RUN=1` ships as the default on purpose.

## 1. Prerequisites

### A staging box you control

The bot runs on a Cut+Dry nonprod staging instance. If you already have one (you do, if you
use `pnpm staging:sync`), use it. If not: push a branch ending in `-staging` and CircleCI
builds the CloudFormation stack — EC2 + ALB + CodeDeploy. See `infrastructure/CLAUDE.md` in
the monolith.

Note your **env slug**: the branch name minus `-staging`. Branch `asela-staging` → `asela`.
You will be asked for it, and every hostname derives from it.

> ⚠️ A branch-based nonprod stack is **ephemeral** — it is torn down when the PR closes or
> the branch is deleted, and `$HOME` goes with it. Re-running `bootstrap.sh` rebuilds
> everything except your two secrets and the Claude sign-in. See
> [OPERATIONS.md](OPERATIONS.md#when-the-box-is-rebuilt).

### A GitHub PAT

A **classic** PAT with the `repo` scope, on your own account:
GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic).

Do **not** add `read:org`. The only call that ever needed it was
`gh pr view --json reviewRequests`, which resolves through GraphQL; the REST endpoints carry
the same data under `repo` alone.

This token acts as **you**. That is the point — it is what makes every comment the bot posts
attributable to a human rather than a bot account.

### A Slack incoming webhook

Create one pointed at a **private, single-member channel**. The cards carry PR titles,
authors and diff sizes, so it should not be a shared channel.

Slack → your workspace apps → Incoming Webhooks → add to a channel → copy the URL.

Optional: leave `SLACK_WEBHOOK` empty and the bot runs fine, you just have to remember to
open the dashboard yourself.

## 2. Get the code onto the box

```bash
# on your laptop — clone this repo somewhere, then copy it over. Or clone it on the box.
AWS_PROFILE=non-prod-sso aws ssm start-session --target <instance-id>
```

On the box:

```bash
sudo su - ubuntu     # NOT ssm-user — Claude Code creds must land in ubuntu's $HOME
git clone git@github.com:GetCodifyAI/claude-pr-review-bot.git ~/claude-pr-review-bot
```

If the box has no GitHub SSH key, clone over HTTPS with your PAT, or `scp` the directory up
from your laptop. It is ~2,200 lines of bash and Python with no build step.

> **Why `~/claude-pr-review-bot` and not the deploy directory?** `pnpm staging:sync` runs
> `rsync --delete` into `/var/local/cut-dry/current/`. Anything under that path can vanish
> mid-review. Keep the bot in `$HOME`.

## 3. Sign Claude Code in

```bash
claude    # with no arguments — shows who you are signed in as
```

If it is not installed, `bootstrap.sh` installs it in the next step and tells you to come
back here. There is no `claude login` subcommand; running `claude` bare walks you through it.

It must be signed in **as the box user** (`ubuntu`), because that is the account the systemd
service and cron jobs run as. Signing in as `ssm-user` puts the credentials in the wrong
`$HOME` and reviews fail with an empty `review.json`.

## 4. Bootstrap

```bash
~/claude-pr-review-bot/bin/bootstrap.sh
```

It prompts for your **GitHub login** and your **staging env slug**, then stops and tells you
the two secrets are still empty. Paste them:

```bash
read -rs PAT  && sed -i "s|^GITHUB_PAT=.*|GITHUB_PAT=$PAT|"    ~/.claude-pr-bot/.env && unset PAT
read -rs HOOK && sed -i "s|^SLACK_WEBHOOK=.*|SLACK_WEBHOOK=$HOOK|" ~/.claude-pr-bot/.env && unset HOOK
```

`read -rs` keeps the secret off your screen and out of shell history. Then re-run to finish:

```bash
~/claude-pr-review-bot/bin/bootstrap.sh
```

Re-running is safe at any point. It only ever **adds** missing keys to `.env`, so your values
are never overwritten — including a `DRY_RUN` you have deliberately flipped to `0`.

What it does: installs `gh` and `claude` if missing, copies the scripts to
`~/.claude-pr-bot/bin/`, clones the monolith as a blobless base clone, installs the
`pr-review` skill to `~/.claude/skills/`, writes and starts the `prbot` systemd unit, adds an
Apache vhost on its own hostname, and installs the cron entry.

It copies itself into `~/.claude-pr-bot/bin/` too, so later runs can use that stable path
rather than the clone.

## 5. Verify

```bash
curl -s localhost:8899/prbot/health            # -> ok
curl -s https://prbot-<your-env>.staging.eng.cutanddry.com/prbot/health   # -> ok, through the ALB
~/.claude-pr-bot/bin/pr-watch.sh               # -> a Slack card per open review request
```

The last one prints `==> notifying #NNNN` for each PR it announces. If you have no open
review requests it prints nothing and that is correct — ask a teammate to add you as a
reviewer on something, or add yourself to any open PR to test.

Click **Open review** on the card. The first visit starts the review; the page reports
`reviewing` and Slack pings you again in 10–15 minutes when it is ready.

## 6. Configuration reference

Everything lives in `~/.claude-pr-bot/.env` (chmod 600).
[`config.example`](../config.example) documents every key with its reasoning.

| Key             | Set by    | Notes                                                          |
| --------------- | --------- | ---------------------------------------------------------------- |
| `GITHUB_PAT`    | you       | Classic PAT, `repo` scope. Acts as **you**                     |
| `SLACK_WEBHOOK` | you       | Private single-member channel. Optional                        |
| `REVIEWER`      | prompt    | Your GitHub login. Must match the PAT's account                |
| `PRBOT_ENV`     | prompt    | Staging env slug. Every hostname derives from it               |
| `REPO`          | default   | `GetCodifyAI/cut-and-dry`                                      |
| `PRBOT_DOMAIN`  | default   | `staging.eng.cutanddry.com`                                    |
| `PRBOT_SECRET`  | generated | Signs every dashboard link                                     |
| `DRY_RUN`       | default 1 | `1` = dashboard works fully but refuses to write to GitHub     |
| `SKIP_BOT_PRS`  | default 0 | `1` ignores PRs authored by bots                               |
| `MIN_FREE_MB`   | default 800 | Refuse to start a review below this much free RAM            |

> **Gotcha:** `prbot-server.py` reads `.env` **once, at startup**. After editing any value —
> especially `DRY_RUN` — run `sudo systemctl restart prbot` or the change silently does
> nothing. `pr-watch.sh` and `run-review.sh` re-source it every run, so only the dashboard
> needs this. Re-running `bootstrap.sh` restarts it for you.

## 7. Rollout — do not skip the dry run

`DRY_RUN=1` is the shipped default. The dashboard renders and every button works; they just
report what *would* have happened.

1. **Dry run.** Point it at PRs you have already reviewed by hand and compare. This is where
   you find out whether the output is good enough to carry your name. Do several.
2. **Your own PRs.** Set `DRY_RUN=0`, `sudo systemctl restart prbot`, and post on a PR you
   authored. Low stakes, real end-to-end.
3. **Live.**

Read [SECURITY.md](SECURITY.md) before step 2. The endpoint is on the public internet.

## Troubleshooting the install

| Symptom                                          | Cause                                                          |
| ------------------------------------------------ | ---------------------------------------------------------------- |
| `Run as ubuntu, not ssm-user`                    | `sudo su - ubuntu` first. Override with `PRBOT_USER=` if your box differs |
| `REVIEWER not set` / `PRBOT_ENV not set`         | First bootstrap ran without a terminal, so the prompts were skipped. Edit `.env` and re-run |
| `apache configtest FAILED`                       | The vhost is auto-disabled and the app is left untouched. Check `sudo apache2ctl configtest` |
| `/prbot unreachable`                             | Bootstrap disables its own vhost rather than leave a broken Apache. Check `sudo systemctl status apache2` |
| Review finishes instantly, empty `review.json`   | Claude Code is not signed in as the box user. `claude` bare, as `ubuntu` |
| `command not found: claude` in `agent.log`       | The systemd unit sets `PATH` to include `~/.local/bin`. Re-run bootstrap to rewrite the unit |
| Slack card never arrives                         | `~/.claude-pr-bot/watch.log`. Then check the cron entry with `crontab -l` |
| Card arrives, button 403s                        | Link expired (7 days) or `PRBOT_SECRET` was rotated. Open the dashboard link instead |
