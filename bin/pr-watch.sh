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

# Don't Slack-nudge for PRs created long ago: a fresh review request on a years-old open PR is
# almost always noise (see the pilot feedback). Such PRs are still marked seen (so they never
# spam) and stay fully visible + reviewable in the dashboard queue — only the Slack ping is
# suppressed. 0 disables the cutoff. Tunable in .env as PRBOT_MAX_PR_AGE_DAYS.
MAX_AGE_DAYS="${PRBOT_MAX_PR_AGE_DAYS:-45}"

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

# --- New-user clean slate ---------------------------------------------------------------------
# A person signing in usually has a backlog of open review requests they'll never action. We
# don't want to Slack-spam them with a card for each, or fill their "To review" with old ones —
# they should start fresh and only see requests from now on. So the FIRST time we ever see a
# login, seed its whole current backlog as already-notified (no Slack) and archive it in the
# dashboard. Only genuinely-new requests after this point ping and land in "To review".
KNOWN="$ROOT/known_logins"
if [ ! -f "$KNOWN" ]; then
  # First run of this logic on an existing box: treat everyone already signed in as onboarded,
  # so upgrading never archives a current user's live queue.
  printf '%s\n' $logins > "$KNOWN"
fi
for login in $logins; do
  grep -qxF "$login" "$KNOWN" && continue
  n=0
  for num in $(jq -r --arg u "$login" \
                 '.[] | select(.requested | index($u)) | .number' "$ROOT/queue.json"); do
    grep -qxF "$num:$login" "$SEEN" || echo "$num:$login" >> "$SEEN"
    mkdir -p "$STATE/$num/users/$login"
    [ -f "$STATE/$num/users/$login/archived" ] || date +%s > "$STATE/$num/users/$login/archived"
    n=$((n + 1))
  done
  echo "$login" >> "$KNOWN"
  echo "==> new user $login: seeded $n backlog PR(s) as seen + archived (clean slate)"
done

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

  # Stale-PR cutoff: suppress the Slack nudge for PRs created more than MAX_AGE_DAYS ago, but
  # still mark them seen so they never re-ping. The dashboard queue is unaffected.
  if [ "${MAX_AGE_DAYS:-0}" -gt 0 ]; then
    created=$(echo "$pr" | jq -r '.createdAt // empty')
    if [ -n "$created" ]; then
      created_s=$(date -d "$created" +%s 2>/dev/null || echo 0)
      if [ "$created_s" -gt 0 ]; then
        age_days=$(( ( $(date +%s) - created_s ) / 86400 ))
        if [ "$age_days" -gt "$MAX_AGE_DAYS" ]; then
          echo "==> #$num created ${age_days}d ago (> ${MAX_AGE_DAYS}d) — marking seen, no ping"
          for login in $new; do echo "$num:$login" >> "$SEEN"; done
          continue
        fi
      fi
    fi
  fi

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
  | slack_post "$num" root

  for login in $new; do echo "$num:$login" >> "$SEEN"; done
done
