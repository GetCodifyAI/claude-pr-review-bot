# Security

Read this before you set `DRY_RUN=0`.

## The shape of the risk

The dashboard is served over the **public internet** with no authentication in front of it.
The nonprod staging ALB answers `*.staging.eng.cutanddry.com` to anyone. Behind it sits a
process holding a GitHub PAT that can comment on, review and approve pull requests **as you**.

So the security model is not "the network protects it". It is: every entry point is signed,
the dangerous ones are short-lived, and the process itself cannot be reached directly.

## Controls

- **Every entry point is HMAC-signed** over `action:pr:expiry` with `PRBOT_SECRET`. Unsigned,
  altered or expired requests get a 403.

- **Two token tiers.** Page links last 7 days — viewing is harmless. Post and approve carry
  their **own 30-minute tokens, minted at render time**, so a bookmarked or forwarded URL
  cannot post or approve later. Getting the page is not getting the button.

- **Approve is gated** on the PR being open, not a draft, not authored by you, and having a
  `review.json` on this box. Deliberately *not* gated on "are you still a requested
  reviewer": GitHub clears the review request the moment any review is submitted, which made
  post-then-approve structurally impossible.

- **A failed GitHub call never degrades into a bad post.** `jq --slurp` wraps an API error so
  that it looks like a page of results; unchecked, every comment would appear un-anchorable
  and get dumped into the summary body. The fetch validates the response shape, retries once,
  and refuses on anything odd.

- **Diff-anchor validation** (`prbot_diff.py`) checks every comment's `path:line` against the
  actual diff before posting, so GitHub cannot 422 the entire review because one finding
  pointed at a line that isn't in the diff.

- **The server binds `127.0.0.1`**, reachable only through the Apache proxy. Nothing is
  listening on a public port directly.

- **The vhost serves nothing but `/prbot`.** `RedirectMatch 404 ^(?!/prbot)` — the hostname
  exposes the dashboard and no part of the app.

- **Secrets stay in `~/.claude-pr-bot/.env`** (chmod 600), outside the rsync target, never in
  a git repo. `.gitignore` here blocks `.env` and `*.pem` as a second line of defence.

- **Nothing reaches GitHub without a human clicking.** `run-review.sh` has no write path to
  GitHub at all.

## Your PAT

It is a classic PAT with `repo` scope, which is broad — it can write to every repo you can
write to, not just the monolith. That is a real cost, accepted because the alternative
(a GitHub App) posts as a bot, and the entire value of this tool is that reviews carry a
human name.

Mitigations that are worth doing:

- **Give it an expiry.** 90 days. Re-pasting it into `.env` twice a year is cheap.
- **Do not add scopes it does not need**, especially `read:org`. See
  [SETUP.md](SETUP.md#a-github-pat) for why it isn't needed.
- **Revoke it the moment the box is decommissioned**, not later. An ephemeral staging box
  that gets torn down leaves a live token behind if you forget.

## Rotating `PRBOT_SECRET`

If a signed link ever leaks somewhere it shouldn't — a shared channel, a screenshot, a
ticket — rotate it:

```bash
sed -i "s|^PRBOT_SECRET=.*|PRBOT_SECRET=$(openssl rand -hex 32)|" ~/.claude-pr-bot/.env
sudo systemctl restart prbot
```

Every outstanding link is immediately invalid, including your own. See
[OPERATIONS.md](OPERATIONS.md#re-notifying-stale-slack-cards) to re-announce your queue.

## Slack

Point the webhook at a **private, single-member channel**. The cards carry PR titles, authors
and diff sizes, and the review verdict card carries the agent's summary — which can quote
code. A shared channel leaks all of that to everyone in it.

Treat the webhook URL itself as a secret: anyone holding it can post into that channel.

## What is not defended against

Stated plainly, so nobody assumes otherwise:

- **Someone with a valid, unexpired page link can read your review.** Findings on a private
  repo's PR, in prose. The 7-day page TTL bounds this; the 30-minute action TTL means they
  still cannot post or approve.
- **Someone with root on the box has everything** — the PAT, the Slack webhook, the Claude
  credentials. It is a nonprod box, so the blast radius is your GitHub account rather than
  production data, but that is not nothing.
- **The agent reads untrusted PR content.** It runs with `Bash` allowed, in a worktree, on a
  nonprod box. A hostile PR could in principle try prompt injection to get the agent to do
  something with those tools. It cannot post to GitHub — `run-review.sh` has no write path —
  but it can run commands on the box. Worth knowing before you point this at PRs from outside
  the team.

## If you find a problem

Open an issue on this repo, or fix it and push — everyone runs the same scripts from their
own box, so a fix propagates to the whole team on their next `bootstrap.sh`.
