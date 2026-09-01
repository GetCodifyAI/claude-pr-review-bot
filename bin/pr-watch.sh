#!/usr/bin/env bash
# pr-watch.sh — poll for PRs awaiting $REVIEWER's review, keep queue.json fresh for the
#               dashboard, and post a Slack card for anything newly requested.
#
# cron (every 3 min, flock'd):
#   */3 * * * * flock -n /tmp/pr-watch.lock $HOME/.claude-pr-bot/bin/pr-watch.sh
#
# Notify only; no review runs from here. Dedup is per PR NUMBER, so a PR is announced once
# and never again — pushing new commits changes the head SHA but must not re-ping you. The
# dashboard always reflects the live queue regardless of what has been announced, so Slack
# is a one-time nudge rather than the source of truth. To re-announce one, drop its line
# from `seen`.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
# shellcheck source=lib-common.sh
. "$(dirname "$0")/lib-common.sh"
require_env

# One search beats paging every open PR: the repo sees 200+ PR updates a week, and
# `review-requested:` already resolves to direct individual requests.
rows=$(gh pr list --repo "$REPO" --state open \
        --search "review-requested:$REVIEWER" --limit 50 \
        --json number,title,author,headRefOid,url,additions,deletions,changedFiles,isDraft,createdAt,updatedAt \
        2>/dev/null) || { echo "gh search failed"; exit 0; }

# queue.json backs the dashboard index. Rewritten every run so the dashboard never has to
# call gh itself — page loads stay instant and the API cost stays at one search per 3 min.
echo "$rows" | jq '[.[] | {number, title, url, additions, deletions, changedFiles,
                           author: .author.login, isBot: (.author.is_bot // false),
                           isDraft, head: .headRefOid, createdAt, updatedAt}]' \
  > "$ROOT/queue.json.tmp" \
  && mv "$ROOT/queue.json.tmp" "$ROOT/queue.json"

echo "$rows" | jq -c '.[]' | while read -r pr; do
  num=$(echo "$pr"    | jq -r .number)
  key="$num"
  grep -qxF "$key" "$SEEN" && continue

  draft=$(echo "$pr"  | jq -r .isDraft)
  [ "$draft" = "true" ] && { echo "$key" >> "$SEEN"; continue; }

  author=$(echo "$pr" | jq -r .author.login)
  is_bot=$(echo "$pr" | jq -r '.author.is_bot // false')
  if [ "$SKIP_BOT_PRS" = "1" ] && [ "$is_bot" = "true" ]; then
    echo "$key" >> "$SEEN"; continue
  fi

  title=$(echo "$pr"  | jq -r .title)
  url=$(echo "$pr"    | jq -r .url)
  adds=$(echo "$pr"   | jq -r .additions)
  dels=$(echo "$pr"   | jq -r .deletions)
  files=$(echo "$pr"  | jq -r .changedFiles)
  detail=$(signed_link pr "$num" 604800)   # 7 days — opening the dashboard costs nothing
  board=$(dashboard_link 604800)

  echo "==> notifying #$num ($author) $title"
  jq -n --arg t "$title" --arg u "$url" --arg a "$author" --arg l "$detail" \
        --arg n "$num" --arg s "$adds" --arg d "$dels" --arg f "$files" --arg b "$board" '
  {blocks: [
    {type:"section", text:{type:"mrkdwn",
      text:("*<" + $u + "|#" + $n + " — " + $t + ">*\n`@" + $a + "`  ·  +" + $s
            + " −" + $d + "  ·  " + $f + " files")}},
    {type:"actions", elements:[
      {type:"button", text:{type:"plain_text", text:"🔍 Open review"},
       style:"primary", url:$l},
      {type:"button", text:{type:"plain_text", text:"Dashboard"}, url:$b},
      {type:"button", text:{type:"plain_text", text:"Open PR"}, url:$u}]}]}' \
  | slack_post

  echo "$key" >> "$SEEN"
done
