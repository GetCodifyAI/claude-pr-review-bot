#!/usr/bin/env bash
# pr-watch.sh — poll for PRs awaiting review from every signed-in user, keep queue.json
#               fresh for the dashboard, and post a Slack card for anything newly requested.
#
# cron (every 3 min, flock'd):
#   */3 * * * * flock -n /tmp/pr-watch.lock $HOME/.claude-pr-bot/bin/pr-watch.sh
#
# Notify only; no review runs from here. Dedup is per PR + LOGIN (`<pr>:<login>` in `seen`),
# so each reviewer is pinged once per PR and never again — pushing new commits changes the
# head SHA but must not re-ping anyone. The dashboard always reflects the live queue
# regardless of what has been announced, so Slack is a one-time nudge rather than the source
# of truth. To re-announce one, drop its line from `seen`.
#
# Users come from users.json (written by the dashboard on sign-in). Only login + slack_id are
# read here — PATs stay encrypted and are only ever decrypted by the server, for posting.
# With no users yet, falls back to polling $REVIEWER alone so a fresh box still works.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
# shellcheck source=lib-common.sh
. "$(dirname "$0")/lib-common.sh"
require_env

USERS_FILE="$ROOT/users.json"
logins=$(jq -r 'keys[]' "$USERS_FILE" 2>/dev/null)
[ -n "$logins" ] || logins="$REVIEWER"

# One search per user beats paging every open PR: the repo sees 200+ PR updates a week, and
# `review-requested:` resolves to direct individual requests server-side. Each row is tagged
# with the login it was found for; rows for the same PR are merged below.
fields=number,title,author,headRefOid,url,additions,deletions,changedFiles,isDraft,createdAt,updatedAt
tagged=""
for login in $logins; do
  rows=$(gh pr list --repo "$REPO" --state open --search "review-requested:$login" \
          --limit 50 --json "$fields" 2>/dev/null) || { echo "gh search failed for $login"; continue; }
  tagged+=$(echo "$rows" | jq -c --arg u "$login" '.[] | . + {requested:[$u]}')$'\n'
done

# queue.json backs the dashboard index. Rewritten every run so the dashboard never has to
# call gh itself. `requested` is the union of logins awaiting each PR — the dashboard filters
# on it, so one file serves every user.
echo "$tagged" | jq -s 'group_by(.number) | map(.[0] + {requested: (map(.requested[]) | unique)})
  | map({number, title, url, additions, deletions, changedFiles, requested,
         author: .author.login, isBot: (.author.is_bot // false),
         isDraft, head: .headRefOid, createdAt, updatedAt})' \
  > "$ROOT/queue.json.tmp" \
  && mv "$ROOT/queue.json.tmp" "$ROOT/queue.json"

# <@U…> pings the person; a bare @login is a visible label that pings nobody, which is what
# you get until you add your Slack member ID in the dashboard's settings.
mention() {
  local sid
  sid=$(jq -r --arg l "$1" '.[$l].slack_id // ""' "$USERS_FILE" 2>/dev/null)
  [ -n "$sid" ] && echo "<@$sid>" || echo "@$1"
}
seen_for() {   # <pr> <login>: legacy bare "<pr>" lines were the owner's
  grep -qxF "$1:$2" "$SEEN" || { [ "$2" = "$REVIEWER" ] && grep -qxF "$1" "$SEEN"; }
}

jq -c '.[]' "$ROOT/queue.json" | while read -r pr; do
  num=$(echo "$pr" | jq -r .number)

  # Who on this PR has not been told yet?
  new=""
  for login in $(echo "$pr" | jq -r '.requested[]'); do
    seen_for "$num" "$login" || new+="$login "
  done
  [ -n "$new" ] || continue

  draft=$(echo "$pr"  | jq -r .isDraft)
  is_bot=$(echo "$pr" | jq -r '.isBot')
  if [ "$draft" = "true" ] || { [ "$SKIP_BOT_PRS" = "1" ] && [ "$is_bot" = "true" ]; }; then
    for login in $new; do echo "$num:$login" >> "$SEEN"; done
    continue
  fi

  author=$(echo "$pr" | jq -r .author)
  title=$(echo "$pr"  | jq -r .title)
  url=$(echo "$pr"    | jq -r .url)
  adds=$(echo "$pr"   | jq -r .additions)
  dels=$(echo "$pr"   | jq -r .deletions)
  files=$(echo "$pr"  | jq -r .changedFiles)
  detail=$(signed_link pr "$num" 604800)   # 7 days — opening the dashboard costs nothing
  board=$(dashboard_link 604800)
  who=""; for login in $new; do who+="$(mention "$login") "; done

  echo "==> notifying #$num ($author) $title → $new"
  jq -n --arg t "$title" --arg u "$url" --arg a "$author" --arg l "$detail" --arg w "$who" \
        --arg n "$num" --arg s "$adds" --arg d "$dels" --arg f "$files" --arg b "$board" '
  {blocks: [
    {type:"section", text:{type:"mrkdwn",
      text:($w + "review requested\n*<" + $u + "|#" + $n + " — " + $t + ">*\n`@" + $a
            + "`  ·  +" + $s + " −" + $d + "  ·  " + $f + " files")}},
    {type:"actions", elements:[
      {type:"button", text:{type:"plain_text", text:"🔍 Open review"},
       style:"primary", url:$l},
      {type:"button", text:{type:"plain_text", text:"Dashboard"}, url:$b},
      {type:"button", text:{type:"plain_text", text:"Open PR"}, url:$u}]}]}' \
  | slack_post

  for login in $new; do echo "$num:$login" >> "$SEEN"; done
done
