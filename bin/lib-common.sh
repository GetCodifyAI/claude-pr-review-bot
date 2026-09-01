#!/usr/bin/env bash
# lib-common.sh — shared config, HMAC link signing, and Slack posting for the review bot.
# Sourced by pr-watch.sh and run-review.sh. Never executed directly.

ROOT="${ROOT:-$HOME/.claude-pr-bot}"
ENV_FILE="$ROOT/.env"

# Secrets live in .env (chmod 600), never in this repo. bootstrap.sh creates it.
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

# --- per-person config -----------------------------------------------------------------------
# All of these come from ~/.claude-pr-bot/.env, which bootstrap.sh writes. REPO has a sane
# default; REVIEWER and PRBOT_ENV are yours alone and have none, so require_env fails loudly
# rather than letting the bot poll for someone else's review requests.
REPO="${REPO:-GetCodifyAI/cut-and-dry}"
REVIEWER="${REVIEWER:-}"                 # your GitHub login; the PAT must belong to it
# Your staging environment slug — the branch name minus `-staging` (branch `asela-staging`
# => `asela`). Every hostname derives from this one value so the two cannot drift apart.
PRBOT_ENV="${PRBOT_ENV:-}"
PRBOT_DOMAIN="${PRBOT_DOMAIN:-staging.eng.cutanddry.com}"
# Own hostname, not app-<env>: the app's vhost swallows every path into index.php. Any
# prefix ending in <env>.staging.eng.cutanddry.com routes here via the nginx staging proxy.
# Record whether PUBLIC_URL came from .env BEFORE the derived default fills it in. Without
# this the value is never empty, so the "did you set PRBOT_ENV?" guard below could never
# fire and an unset slug shipped Slack buttons pointing at https://prbot-.staging...
PUBLIC_URL_EXPLICIT="${PUBLIC_URL:+1}"
PRBOT_HOST="${PRBOT_HOST:-prbot-${PRBOT_ENV}.${PRBOT_DOMAIN}}"
PUBLIC_URL="${PUBLIC_URL:-https://$PRBOT_HOST}"
BASE="$ROOT/repo"                 # base clone; review worktrees branch off it
WT="$ROOT/wt"
STATE="$ROOT/state"               # per-PR job state, one dir per PR
SEEN="$ROOT/seen"                 # notified review requests, keyed <pr>:<head-sha>
USED="$ROOT/used-nonces"          # burned approve links (single-use enforcement)

# Skip bot-authored PRs when 1. Default 0: AI-written PRs are where a skeptical
# review pays off most, and they're the bulk of the queue.
SKIP_BOT_PRS="${SKIP_BOT_PRS:-0}"
# When 1, review but never touch GitHub — Slack only. Phase 3 starts here.
DRY_RUN="${DRY_RUN:-1}"
# Refuse to start a review below this much available RAM (MB). The box is a
# staging box is small (a t4g.medium is 3.8 GB) and also runs Apache + webpack watch.
MIN_FREE_MB="${MIN_FREE_MB:-800}"

mkdir -p "$WT" "$STATE"; touch "$SEEN" "$USED"

# gh + git both authenticate as $REVIEWER via the PAT, so every comment, review,
# and approval on GitHub is attributed to the human, not a bot account.
export GH_TOKEN="${GITHUB_PAT:-}"

die() { echo "FATAL: $*" >&2; exit 1; }

require_env() {
  [ -n "${GITHUB_PAT:-}" ]   || die "GITHUB_PAT not set in $ENV_FILE"
  [ -n "${PRBOT_SECRET:-}" ] || die "PRBOT_SECRET not set in $ENV_FILE"
  [ -n "${REVIEWER:-}" ]     || die "REVIEWER not set in $ENV_FILE (your GitHub login)"
  # Without this the derived host is `prbot-.staging...` — a link that resolves nowhere and
  # fails only later, in Slack, as a dead button. Catch it at the source instead.
  [ -n "${PRBOT_ENV:-}" ] || [ -n "${PUBLIC_URL_EXPLICIT:-}" ] \
    || die "PRBOT_ENV not set in $ENV_FILE (your staging env slug, e.g. asela)"
}

# --- signed links ----------------------------------------------------------------------------
# The /prbot endpoint is served over the PUBLIC internet (the staging ALB answers
# *.staging.eng.cutanddry.com with no auth in front), so every link carries an
# HMAC over action+pr+expiry. Unsigned or expired links are rejected server-side.
sign() { printf '%s' "$1" | openssl dgst -sha256 -hmac "$PRBOT_SECRET" -r | cut -d' ' -f1; }

# signed_link <action> <pr> <ttl-seconds>
signed_link() {
  local action="$1" pr="$2" ttl="$3" exp sig
  exp=$(( $(date +%s) + ttl ))
  sig=$(sign "$action:$pr:$exp")
  echo "$PUBLIC_URL/prbot/$action?pr=$pr&exp=$exp&sig=$sig"
}

# dashboard_link [ttl-seconds] — the index page; signed with empty action and pr.
dashboard_link() {
  local ttl="${1:-604800}" exp sig
  exp=$(( $(date +%s) + ttl ))
  sig=$(sign "::$exp")
  echo "$PUBLIC_URL/prbot/?exp=$exp&sig=$sig"
}

# --- slack -----------------------------------------------------------------------------------
# Incoming webhooks are send-only and do not return a message ts, so replies cannot
# be threaded. Each update is a fresh message that names the PR for correlation.
slack_post() {
  [ -n "${SLACK_WEBHOOK:-}" ] || { echo "(no SLACK_WEBHOOK; skipping notify)"; return 0; }
  curl -fsS -X POST -H 'Content-type: application/json' \
    --data @- "$SLACK_WEBHOOK" >/dev/null 2>&1 || echo "WARN: slack post failed" >&2
}

have_free_mem() {
  local free_mb
  free_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
  [ "${free_mb:-0}" -ge "$MIN_FREE_MB" ]
}
