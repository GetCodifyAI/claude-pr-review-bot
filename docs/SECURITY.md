# Security

Read this before you set `DRY_RUN=0`.

## The shape of the risk

The dashboard is served over the **public internet** with no authentication in front of it.
The nonprod staging ALB answers `*.staging.eng.cutanddry.com` to anyone. Behind it sits a
process holding a GitHub PAT that can comment on, review and approve pull requests **as you**.

So the security model is not "the network protects it". It is: every entry point is signed,
the dangerous ones are short-lived, and the process itself cannot be reached directly.

## Controls

- **Pages need a signed-in session.** Signing in means proving a GitHub PAT is real (`/user`)
  and can see the repo, then receiving an HMAC-signed, HttpOnly, Secure, SameSite cookie
  valid for 30 days. Unauthenticated requests — including POSTs — bounce to the login page.
  Deleting a user from `users.json` invalidates their session on the next request.

- **GitHub login tokens are GitHub's, not pasted.** The OAuth `state` is HMAC-signed with a
  10-minute expiry and carries only an in-`/prbot/` return path, so a forged or replayed
  callback is rejected and cannot redirect off-site. Tokens (and refresh tokens, for a GitHub
  App) are stored exactly like PATs below, and refreshed server-side a minute before expiry.

- **Stored PATs are encrypted at rest** (AES-256-CBC, PBKDF2) with a key *derived* from
  `PRBOT_SECRET`, not stored beside them. Rotating the secret therefore also invalidates every
  stored PAT — the right outcome if the secret was rotated because it leaked. The shell
  scripts only ever read login + Slack ID; decryption happens in the server, at post time.

- **Writes use the acting user's own PAT.** The service token in `.env` does reads and the
  base clone only. Nothing can post or approve under a name other than the signed-in user's,
  and GitHub's own self-approval check runs against that user.

- **Every action is HMAC-signed** over `action:pr:expiry` with `PRBOT_SECRET` — post, approve,
  mark-done, archive, start-review. Tokens are **minted at render time and last 30 minutes**,
  so a bookmarked or forwarded page cannot act later, and a cross-site form has nothing valid
  to present. Pages themselves are gated by the session, not a signature, so they are plain
  bookmarkable URLs.

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

## Your PAT — and now your teammates'

Every signed-in user's PAT sits on this box, encrypted, decryptable by anyone with root and
the `.env`. For a pilot among a trusted team on a non-prod box, with each person able to
revoke their own token at any time, that is an accepted trade. It is **not** the end state:
the GitHub App upgrade (user-to-server tokens, 8-hour expiry, org-owner revocation, no paste)
replaces this layer without touching the review or dashboard logic. Tell pilots to give
their PAT a 90-day expiry.

It is a classic PAT with `repo` scope, which is broad — it can write to every repo you can
write to, not just the monolith. That is the cost of shipping today without an org owner's
approval; it is not because human attribution needs it. A GitHub App's *user-to-server*
token keeps the human's name on every comment with an 8-hour lifetime and per-app scope.

Mitigations that are worth doing:

- **Give it an expiry.** 90 days. Re-pasting it in Settings twice a year is cheap.
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

Point the webhook at a **private channel containing only the pilot group**. Cards `@mention`
the requested reviewer, so one channel serves everyone. The cards carry PR titles, authors and
diff sizes, and the review-ready card carries the agent's summary — which can quote code.
Everyone in the channel can already read the repo; do not widen it beyond that.

Treat the webhook URL itself as a secret: anyone holding it can post into that channel.

## What is not defended against

Stated plainly, so nobody assumes otherwise:

- **Any signed-in user can read any review on the box.** The review is shared by design; PR
  detail pages are not scoped to who was requested. Everyone signed in has repo access
  anyway, so this discloses nothing they could not `gh pr diff`.
- **Someone with root on the box has everything** — every stored GitHub and Claude token, the
  Slack webhook, the owner's Claude credentials. It is a nonprod box, so the blast radius is your GitHub account rather than
  production data, but that is not nothing.
- **The agent reads untrusted PR content.** It runs with `Bash` allowed, in a worktree, on a
  nonprod box. A hostile PR could in principle try prompt injection to get the agent to do
  something with those tools. It cannot post to GitHub — `run-review.sh` has no write path —
  but it can run commands on the box. Worth knowing before you point this at PRs from outside
  the team.

## If you find a problem

Open an issue on this repo, or fix it and push — everyone runs the same scripts from their
own box, so a fix propagates to the whole team on their next `bootstrap.sh`.
