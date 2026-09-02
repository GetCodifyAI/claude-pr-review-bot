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

## Team pilot

One box, many reviewers. The owner does steps 1–5 above once; everyone else does this:

### GitHub login — one click instead of a token (recommended)

Create an app, paste two values into `.env`, restart. Teammates then see **Sign in with
GitHub** and never touch a token. Two kinds of app; start with the first:

**Option A — OAuth App (no org installation needed).** github.com → Settings → Developer
settings → OAuth Apps → *New OAuth App*:

| Field                      | Value                                                            |
| -------------------------- | ---------------------------------------------------------------- |
| Application name           | `PR review bot`                                                  |
| Homepage URL               | `https://prbot-<env>.staging.eng.cutanddry.com/prbot/`           |
| Authorization callback URL | `https://prbot-<env>.staging.eng.cutanddry.com/prbot/oauth/callback` |
| Enable Device Flow         | off                                                              |
| Expire user access tokens  | **on** — 8-hour tokens with refresh; the dashboard refreshes them itself |

Register, copy the **Client ID**, click *Generate a new client secret* (shown once), then on
the box:

```bash
sed -i "s|^GH_CLIENT_ID=.*|GH_CLIENT_ID=<client id>|; s|^GH_CLIENT_SECRET=.*|GH_CLIENT_SECRET=<secret>|; s|^GH_OAUTH_SCOPES=.*|GH_OAUTH_SCOPES=repo|" ~/.claude-pr-bot/.env
sudo systemctl restart prbot
```

Teammates click *Sign in with GitHub* → GitHub's *Authorize* screen → back to the dashboard,
landing on Settings the first time so they add their Slack member ID. The token GitHub issues
acts as them (comments carry their name), lasts 8 hours and is refreshed server-side before
it lapses, and they can revoke the app any time at github.com/settings/applications.

> If the sign-in comes back with **"the token cannot see GetCodifyAI/cut-and-dry"**, the org
> has *third-party OAuth application access restrictions* on. An org owner approves the app
> once (Org settings → Third-party access → the pending request) and it works for everyone
> from then on. Token sign-in keeps working meanwhile.

**Option B — GitHub App (narrower permissions, needs an org owner to install it).** Same
callback URL; permissions *Pull requests: Read & write*, *Contents: Read*; enable *Request user
authorization (OAuth) during installation*; leave `GH_OAUTH_SCOPES` **empty**. What it adds
over Option A is scope: app-level permissions instead of the user's full `repo`, and org-owner
revocation. Comments show the user's avatar with the app's badge. Ask an owner to install it on `GetCodifyAI` — until then the sign-in fails
with the same message as above. This is the end-state; Option A is the way to be live today.

### Teammate — under two minutes

1. Open `https://prbot-<env>.staging.eng.cutanddry.com/prbot/login` (the owner sends you the
   link).
2. **Sign in with GitHub** if the button is there. Otherwise the token path is two clicks:
   *Create token on GitHub ↗* opens GitHub with the scope and name already filled in — pick
   an expiry, *Generate token*, copy — then paste it. The page tells you as you paste whether
   it looks right, and checks it with GitHub on submit. The token is stored encrypted and used
   only to post the comments and approvals *you* choose, as *you*.
3. You land on a **welcome checklist**: paste your **Slack member ID** (Slack → your profile
   picture → Profile → ⋮ → *Copy member ID*) so review requests ping you, and optionally
   connect your own Claude account. *Skip for now* is fine; both live in *Settings*.

Done. The next time someone requests your review, you get a card in the shared channel
within 3 minutes.

### Owner — one-time

- Create a **private Slack channel** (e.g. `#pr-review-bot`), invite the pilot group, and
  point `SLACK_WEBHOOK` at it. Cards `@mention` whoever is requested, so one channel serves
  everyone; only the mentioned person is pinged.
- Sign in yourself at `/prbot/login` like everyone else. Your pre-multi-user history
  (`state/<pr>/posted.json` etc.) is read as yours automatically.
- Reviews run on this box's Claude sign-in unless the person starting one has connected
  their own account (below), and **serialize** — one at a time, box-wide — so two agents never
  share a small box. Queued reviews show as `queued — waiting…` on their page. To take
  yourself out of the billing path entirely, set `ANTHROPIC_API_KEY` in the systemd unit's
  environment; `claude -p` honours it for anyone who has not connected their own account.

### Connect your own Claude account (optional, per person)

By default every review runs on the box's shared Claude sign-in — the owner's subscription.
Anyone can move *their* reviews onto their own account:

1. On your laptop: `claude setup-token`. It opens the browser, you sign in with your Claude
   account, and it prints a long-lived token.
2. Dashboard → *Settings* → paste it under **Claude** → *Save*. It is verified with one tiny
   call and stored encrypted, like your GitHub token.

From then on, reviews **you** click to start run with your token, billed to your plan and
subject to your plan's limits. Reviews other people start are unaffected; a review is shared
per PR, so whoever starts it pays for it and everyone requested reads it. The PR page says
which account it ran on. *Disconnect* in Settings reverts you to the shared runner.

There is no "Sign in with Anthropic" button because Anthropic offers no third-party login;
`claude setup-token` is the mechanism it documents for handing a subscription to automation
(it is how the official GitHub Action authenticates). The token is honoured over the box's
own sign-in — verified: a bad one fails with a `401` rather than silently using the owner's.

### What each person sees

| Tab / page  | Scope                                                          |
| ----------- | -------------------------------------------------------------- |
| To review   | PRs currently awaiting **your** review                         |
| Reviewed    | Reviews you have opened that you have not posted yet           |
| Posted      | Posted by **you** (someone else posting the same PR is theirs) |
| Approved    | Approved by **you**                                            |
| PR detail   | The shared review; your own tick/edit state and actions        |

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

Per-person data is not in `.env`. It lives in `~/.claude-pr-bot/users.json` (chmod 600):
`{login: {pat_enc, slack_id, name, added}}`, written by the dashboard on sign-in. PATs are
AES-256 encrypted with a key derived from `PRBOT_SECRET`. To remove someone, delete their
key from that file — their session dies on the next request.

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
