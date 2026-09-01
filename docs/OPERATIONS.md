# Operations

## Everyday commands

```bash
systemctl status prbot                        # endpoint health
journalctl -u prbot -f                        # endpoint log
tail -f ~/.claude-pr-bot/watch.log            # poller log
cat ~/.claude-pr-bot/state/<pr>/agent.log     # what the agent did on one PR
cat ~/.claude-pr-bot/state/<pr>/review.json   # the raw agent output
cat ~/.claude-pr-bot/state/<pr>/status        # fetching / reviewing / done / failed
~/.claude-pr-bot/bin/run-review.sh <pr>       # run a review by hand
~/.claude-pr-bot/bin/pr-watch.sh              # poll now instead of waiting for cron
curl -s localhost:8899/prbot/health           # -> ok
```

## The endpoint caches `.env`

`prbot-server.py` reads `~/.claude-pr-bot/.env` **once, at startup**. After editing any
secret — or flipping `DRY_RUN` — restart it or the change silently does nothing:

```bash
sudo systemctl restart prbot
```

This is the single most common "why isn't it working" cause. `pr-watch.sh` and
`run-review.sh` re-source `.env` on every run, so only the dashboard needs this. Re-running
`bootstrap.sh` restarts it for you.

## Re-notifying stale Slack cards

Slack dedup is keyed on PR number in `~/.claude-pr-bot/seen`. To re-announce PRs whose cards
went stale — after a link expiry, or a secret rotation — drop their lines. Keeping the lines
for PRs that already have a `review.json` avoids re-notifying work you already finished:

```bash
R=~/.claude-pr-bot
while read -r l; do [ -f "$R/state/${l%%:*}/review.json" ] && echo "$l"; done < $R/seen > /tmp/s
mv /tmp/s $R/seen && $R/bin/pr-watch.sh
```

To re-announce one specific PR: `sed -i '/^1234$/d' ~/.claude-pr-bot/seen`.

## A review is stuck on "reviewing"

Check `~/.claude-pr-bot/state/<pr>/agent.log`. Common causes:

- **Claude Code is not signed in** as the box user → an empty `review.json`.
- **`claude` not on PATH** — the native installer puts it in `~/.local/bin`, which is not on
  systemd's default PATH. The unit sets it explicitly; re-run `bootstrap.sh` if the unit is
  stale.
- **The 25-minute timeout hit.** Very large PRs do this. Re-run by hand and watch the log.
- **The box ran out of memory.** Reviews refuse to start below `MIN_FREE_MB` (default 800),
  but a review already in flight can still be OOM-killed. The box also runs Apache and
  webpack watch.
- **You deployed mid-review.** The systemd unit sets `KillMode=process` specifically so that
  stopping the service does not reap the detached `run-review.sh` it spawned — but a full box
  restart still will.

Clear a stuck one by re-running: `~/.claude-pr-bot/bin/run-review.sh <pr>`. It takes a
`flock` per PR, so a genuinely-running review will not be duplicated.

## When the box is rebuilt

A branch-based nonprod stack is ephemeral. If the instance is replaced, `$HOME` goes with it
— the base clone, the state, the secrets, and the Claude sign-in. Old Slack links keep
pointing at a hostname that now resolves to a box with no bot on it, so buttons 403 or hang.

To recover:

1. `sudo su - ubuntu`, re-clone this repo.
2. Sign Claude Code back in (`claude`, bare).
3. Re-run `bootstrap.sh`, paste the two secrets, re-run it again.
4. `PRBOT_SECRET` is regenerated, so **every outstanding Slack link is now invalid**. Clear
   `seen` (see above) to re-announce your open queue.

Everything else rebuilds itself. Nothing needs to be restored from a backup.

## Known limits

- **Slack replies are not threaded.** Incoming webhooks are send-only and never return a
  message `ts`, so the verdict arrives as a new message naming the PR rather than a thread
  reply. Threading needs a full Slack app, which the workspace gates.
- **The skill is a copy.** The box runs `~/.claude/skills/pr-review/`, installed by
  bootstrap. Editing it on your laptop does not propagate — commit it here and re-run
  bootstrap on the box.
- **Team review requests are not polled.** `review-requested:<you>` matches direct requests
  only; a request routed through a team handle never fires.
- **Reviews cost tokens.** Roughly 10–15 minutes of agent time on a 25-file PR, against your
  Claude subscription. That is what click-to-run is for.
- **One box, one reviewer.** The whole design assumes the box belongs to you and posts as
  you. Two people cannot share an instance.

## Changing the code

The scripts run from `~/.claude-pr-bot/bin/`, which bootstrap populates. Editing the clone
does nothing until you re-run bootstrap:

```bash
cd ~/claude-pr-review-bot && git pull && bin/bootstrap.sh
```

Bootstrap installs itself into `~/.claude-pr-bot/bin/` too, and detects when it is running
from there so it does not try to install a file onto itself.

If you improve something, push it here — everyone on the team is running the same scripts
from their own box, so a fix helps all of them.
