#!/usr/bin/env bash
# run-review.sh <pr-number> — review one PR and park the result for the dashboard.
#
# Spawned detached by prbot-server.py when "Open review" / "Re-run" is clicked. Writes
# progress to $STATE/<pr>/status so the detail page can report it.
#
# This script NEVER writes to GitHub. It produces review.json; the human then selects and
# edits findings in the dashboard and posts from there. Approval is a separate click again.
set -uo pipefail
. "$(dirname "$0")/lib-common.sh"
require_env

PR="${1:?usage: run-review.sh <pr-number>}"
DIR="$STATE/$PR"
mkdir -p "$DIR"
exec 9>"$DIR/.lock"
flock -n 9 || { echo "review for #$PR already running"; exit 0; }

status() { echo "$1" > "$DIR/status"; echo "[#$PR] $1"; }
fail() { status "failed: $1"; notify_fail "$1"; exit 1; }

notify_fail() {
  jq -n --arg p "$PR" --arg m "$1" --arg u "https://github.com/$REPO/pull/$PR" '
    {blocks:[{type:"section",text:{type:"mrkdwn",
      text:("⚠️ Review of *<" + $u + "|#" + $p + ">* failed: " + $m)}}]}' | slack_post
}

have_free_mem || fail "not enough free memory to start a review"

status "fetching"
meta=$(gh pr view "$PR" --repo "$REPO" \
        --json headRefName,headRefOid,title,url,author,createdAt,updatedAt,additions,deletions,changedFiles \
        2>/dev/null) || fail "PR not found"
branch=$(echo "$meta" | jq -r .headRefName)
title=$(echo "$meta"  | jq -r .title)
url=$(echo "$meta"    | jq -r .url)
# Cache the PR's identity next to the review. queue.json only holds PRs currently awaiting
# review, so once you submit (or the request moves to someone else) the PR drops out of it —
# without this the dashboard would lose the title of a review you just ran.
echo "$meta" | jq --arg n "$PR" '{number:($n|tonumber), title, url,
     author:.author.login, createdAt, updatedAt, additions, deletions, changedFiles}' \
  > "$DIR/meta.json"

# Base clone lives under $ROOT, deliberately NOT the rsync target
# (/var/local/cut-dry/current/) — `staging:dev`'s --delete would otherwise wipe a
# worktree mid-review.
git -C "$BASE" fetch -q origin "$branch" || fail "could not fetch $branch"
wt="$WT/$PR"
git -C "$BASE" worktree remove --force "$wt" 2>/dev/null || true
git -C "$BASE" worktree add -q --force -B "review-$PR" "$wt" "origin/$branch" \
  || fail "could not create worktree"

# One review at a time, box-wide. The per-PR lock above stops duplicates of the SAME review;
# this one stops two DIFFERENT reviews from sharing a box that OOMs with two agents on it.
# The dashboard shows "reviewing" (the per-PR lock is held) with this text as the status.
status "queued — waiting for another review to finish"
exec 8>"$ROOT/review.lock"
flock 8

status "reviewing"
rm -f "$wt/review.json"
(cd "$wt" && timeout 25m claude -p "Use the pr-review skill to review PR #$PR of $REPO.

Follow Step 7 (automation mode): do NOT print the bottom table, do NOT post anything to
GitHub, and write your findings to ./review.json in exactly the shape Step 7 specifies —
event, summary, explainer, analysis, and a comments array whose entries carry path, line,
severity, body and reply_to.

A human reads explainer and analysis in a dashboard to decide whether to trust the findings,
then selects, edits and posts individual comments. So: write that prose for a person, set
reply_to honestly from your Step 1 catalog of existing threads, and keep findings few and
high-confidence." \
  --allowedTools "Bash Read Glob Grep Write" < /dev/null) >"$DIR/agent.log" 2>&1

[ -s "$wt/review.json" ] || fail "agent produced no review.json (see $DIR/agent.log)"
jq -e . "$wt/review.json" >/dev/null 2>&1 || fail "review.json is not valid JSON"
# Copy out before the worktree is removed — this is the artefact the dashboard renders.
cp "$wt/review.json" "$DIR/review.json"
git -C "$BASE" worktree remove --force "$wt" 2>/dev/null || true

# Everyone this PR is awaiting gets the ready ping — the review is shared, only the posting
# is per person. Slack member IDs come from users.json; fall back to the owner.
who=""
for login in $(jq -r --arg n "$PR" '.[] | select((.number|tostring)==$n) | .requested[]?' \
                  "$ROOT/queue.json" 2>/dev/null); do
  sid=$(jq -r --arg l "$login" '.[$l].slack_id // ""' "$ROOT/users.json" 2>/dev/null)
  who+="${sid:+<@$sid> }"
done
[ -n "$who" ] || who="<@$(jq -r --arg l "$REVIEWER" '.[$l].slack_id // ""' "$ROOT/users.json" 2>/dev/null)> "
[ "$who" = "<@> " ] && who=""

event=$(jq -r '.event // "COMMENT"' "$DIR/review.json")
n=$(jq '.comments | length' "$DIR/review.json")
blockers=$(jq '[.comments[]? | select(.severity == "blocker")] | length' "$DIR/review.json")
summary=$(jq -r '.summary // ""' "$DIR/review.json" | head -c 2500)
detail=$(signed_link pr "$PR" 604800)
icon=$([ "$event" = "REQUEST_CHANGES" ] && echo "🔴" || echo "🟢")
status "done ($n findings)"

jq -n --arg t "$title" --arg u "$url" --arg p "$PR" --arg s "$summary" --arg e "$event" \
      --arg i "$icon" --arg n "$n" --arg b "$blockers" --arg l "$detail" --arg w "$who" '
{blocks:[
  {type:"section", text:{type:"mrkdwn",
    text:($w + $i + " Review ready — *<" + $u + "|#" + $p + " — " + $t + ">*\n*" + $e
          + "* · " + $n + " finding(s), " + $b + " blocker(s)")}},
  {type:"section", text:{type:"mrkdwn", text:$s}},
  {type:"actions", elements:[
    {type:"button", text:{type:"plain_text", text:"📋 Open dashboard"},
     style:"primary", url:$l},
    {type:"button", text:{type:"plain_text", text:"Open PR"}, url:$u}]},
  {type:"context", elements:[{type:"mrkdwn",
    text:"Nothing posted yet — select, edit and post from the dashboard."}]}]}' | slack_post
