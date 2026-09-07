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
      text:("⚠️ Review of *<" + $u + "|#" + $p + ">* failed: " + $m)}}]}' | slack_post "$PR" reply
}

have_free_mem || fail "not enough free memory to start a review"
# Reviews run on the clicker's OWN Claude account — never the shared box login. The dashboard
# gates on this, so this is defence in depth.
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || fail "connect your Claude account in the dashboard to review"

# Review effort — how deep the agent goes. The dashboard sets PRBOT_EFFORT (auto-sized from the
# diff, human-overridable). It changes only two things: the timeout, and a depth instruction
# appended to the prompt. Everything else about the run is identical.
EFFORT="${PRBOT_EFFORT:-standard}"
# The dashboard passes the depth instruction (PRBOT_DEPTH), editable per team on the Skills page.
# The built-in text here is only a fallback for a direct/older invocation. Only the timeout is
# decided by the level.
case "$EFFORT" in
  quick) TIMEOUT=12m; FALLBACK="Effort: QUICK — look only at the diff, report clear bugs, be fast.";;
  deep)  TIMEOUT=40m; FALLBACK="Effort: DEEP — search the whole repo for impact, trace data flow, cover perf/security.";;
  *)     EFFORT=standard; TIMEOUT=25m; FALLBACK="Effort: STANDARD — changed files + context, correctness and clear risks.";;
esac
DEPTH="${PRBOT_DEPTH:-$FALLBACK}"
# Optional model override chosen at trigger time (validated server-side). Empty = account default.
MODEL="${PRBOT_MODEL:-}"
MODEL_ARG=()
[ -n "$MODEL" ] && MODEL_ARG=(--model "$MODEL")
echo "$EFFORT" > "$DIR/effort"

status "fetching"
meta=$(gh pr view "$PR" --repo "$REPO" \
        --json headRefName,headRefOid,title,url,author,createdAt,updatedAt,additions,deletions,changedFiles,files \
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
# Record the head SHA this review ran against, so the dashboard can flag the review as stale
# once the author pushes new commits (a new head SHA) — without auto-spending tokens to re-run.
echo "$meta" | jq -r .headRefOid > "$DIR/head"
# Domain-risk flags for a context banner in the dashboard — a path-based heuristic that says
# "this touches money / catalog / DP-integration code, look harder". Never a gate, never routing.
paths=$(echo "$meta" | jq -r '.files[]?.path // empty' 2>/dev/null)
risk=""
printf '%s\n' "$paths" | grep -qiE 'pric|/cost|discount|promo|rebate|margin|/fees?/|Fee|PricingService|OGPrice|OrderGuide|DraftPricing|ZeroPriced|UnitPrice|LocationPriceSync|pricingEngine|dateBasedPricing|SupplierLitePricing' && risk+="pricing "
printf '%s\n' "$paths" | grep -qiE 'catalog|[Pp]roduct|elasticsearch|opensearch|ProductSearch|categor' && risk+="catalog "
printf '%s\n' "$paths" | grep -qiE 'integrators/|SyncLibs|dpCode|vendorId|VerifiedVendor|IntegrationData' && risk+="dp "
echo "$risk" | xargs > "$DIR/risk" 2>/dev/null || true

# Base clone lives under $ROOT, deliberately NOT the rsync target
# (/var/local/cut-dry/current/) — `staging:dev`'s --delete would otherwise wipe a
# worktree mid-review.
status "checking out the branch"
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

status "reviewing the diff"
# Whose Claude account this runs on: the dashboard sets PRBOT_RUN_AS (and, for a connected
# user, CLAUDE_CODE_OAUTH_TOKEN) when it spawns us. Recorded so the page can say so.
echo "${PRBOT_RUN_AS:-shared}" > "$DIR/runner"
echo "[#$PR] running on: ${PRBOT_RUN_AS:-shared}"
rm -f "$wt/review.json"
# Learnings: findings reviewers have dropped as noise or reworded on this repo, so the agent
# stops re-raising rejected ones. Empty on a fresh box. Rendered by prbot_learn.py (beside us).
HERE="$(cd "$(dirname "$0")" && pwd)"
LEARN=$(PYTHONPATH="$HERE" ROOT="$ROOT" python3 -c \
  'import prbot_learn,sys;sys.stdout.write(prbot_learn.render())' 2>/dev/null)

# Which review skill: the clicker's own if they brought one, else the editable team default
# ($ROOT/skills/_global.md, maintained from the dashboard), else the installed pr-review skill.
# Record the id next to the review so learnings can score each skill by how many findings get kept.
ACTOR="${PRBOT_ACTOR:-}"
USER_SKILL="$ROOT/skills/$ACTOR.md"
GLOBAL_SKILL="$ROOT/skills/_global.md"
# The dashboard's active-skill choice: "own" uses the clicker's skill if present, "team" forces
# the shared default even when they have their own on file.
CHOICE="${PRBOT_SKILL_CHOICE:-own}"
FOCUS="${PRBOT_FOCUS:-}"
FOCUSBLOCK=""
[ -n "$FOCUS" ] && FOCUSBLOCK="

The reviewer specifically asked you to focus on the following — prioritise it alongside the skill,
and if it turns out not to apply, say so briefly in the analysis:
$FOCUS"

# Stack context (PRBOT_STACK): when this PR is part of a stack, the diff shows only its own
# changes, so tell the agent the sibling PRs exist to avoid false "undefined/missing" findings.
STACK="${PRBOT_STACK:-}"
STACKBLOCK=""
[ -n "$STACK" ] && STACKBLOCK="

$STACK"
# The output contract — spelled out here so ANY skill (custom or global) yields the exact
# review.json the dashboard needs, independent of whether the skill itself defines the format.
CONTRACT="Do NOT print a table and do NOT post anything to GitHub. Write your findings to
./review.json as a single JSON object: {\"event\":\"COMMENT\", \"summary\":\"…\", \"explainer\":
\"what this PR does\", \"analysis\":\"what you checked and what you dropped\", \"comments\":[{
\"path\":\"file\", \"line\":123, \"severity\":\"blocker|should-fix|nit|question\", \"body\":
\"markdown comment\", \"reply_to\":null, \"suggestion\":null, \"confidence\":\"high|medium|low\"}]}.
Set \"confidence\" to how sure you are the finding is real and worth raising — low-confidence
findings are shown to the reviewer in a separate collapsed \"maybe\" tray, so use it honestly
rather than dropping a borderline point. When a finding has a concrete,
correct fix that replaces the SINGLE line you set in \"line\", put the exact replacement line
(matching its indentation) in \"suggestion\" — the reviewer can post it as a one-click GitHub
suggestion. Only when confident and single-line; otherwise leave \"suggestion\" null. A human
reads summary/explainer/analysis in a dashboard, then selects, edits and posts individual
comments — write that prose for a person and keep findings few and high-confidence.${LEARN}"

if [ "$CHOICE" != team ] && [ -n "$ACTOR" ] && [ -f "$USER_SKILL" ]; then
  echo "$ACTOR" > "$DIR/skill"; APPROACH="$(cat "$USER_SKILL")"
elif [ -f "$GLOBAL_SKILL" ]; then
  echo "global" > "$DIR/skill"; APPROACH="$(cat "$GLOBAL_SKILL")"
else
  echo "global" > "$DIR/skill"; APPROACH=""
fi

if [ -n "$APPROACH" ]; then
  PROMPT="Review PR #$PR of $REPO. Follow this reviewing approach:

$APPROACH
$DEPTH
$FOCUSBLOCK$STACKBLOCK

$CONTRACT"
else
  PROMPT="Use the pr-review skill to review PR #$PR of $REPO. Follow its Step 7 automation mode.
$DEPTH
$FOCUSBLOCK$STACKBLOCK
${CONTRACT}"
fi

(cd "$wt" && timeout "$TIMEOUT" claude -p "$PROMPT" \
  ${MODEL_ARG[@]+"${MODEL_ARG[@]}"} \
  --output-format stream-json --verbose \
  --allowedTools "Bash Read Glob Grep Write" < /dev/null) >"$DIR/agent.log" 2>&1

[ -s "$wt/review.json" ] || fail "agent produced no review.json (see $DIR/agent.log)"
jq -e . "$wt/review.json" >/dev/null 2>&1 || fail "review.json is not valid JSON"
# Copy out before the worktree is removed — this is the artefact the dashboard renders.
cp "$wt/review.json" "$DIR/review.json"

# Token usage + model, parsed from the stream-json log. Best-effort: if anything is missing or
# unparseable we simply write no usage.json and the dashboard omits the usage line.
usage_line=$(grep -a '"type":"result"' "$DIR/agent.log" | tail -1 || true)
init_line=$(grep -a '"subtype":"init"' "$DIR/agent.log" | head -1 || true)
if [ -n "$usage_line" ]; then
  model=$(printf '%s' "$init_line" | jq -r '.model // empty' 2>/dev/null || true)
  [ -n "$model" ] || model=$(printf '%s' "$usage_line" \
      | jq -r '(.modelUsage // {}) | keys[0] // empty' 2>/dev/null || true)
  printf '%s' "$usage_line" | jq -c --arg model "${model:-unknown}" '{
      model: $model,
      input_tokens: (.usage.input_tokens // 0),
      output_tokens: (.usage.output_tokens // 0),
      cache_read_input_tokens: (.usage.cache_read_input_tokens // 0),
      cache_creation_input_tokens: (.usage.cache_creation_input_tokens // 0),
      cost_usd: (.total_cost_usd // 0),
      duration_ms: (.duration_ms // 0)
    }' > "$DIR/usage.json" 2>/dev/null || rm -f "$DIR/usage.json"
fi
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
    text:"Nothing posted yet — select, edit and post from the dashboard."}]}]}' | slack_post "$PR" reply
