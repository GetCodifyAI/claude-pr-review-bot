#!/usr/bin/env bash
# bootstrap.sh — install the review bot on a staging box. Idempotent; safe to re-run.
#
#   sudo su - ubuntu            # NOT ssm-user: claude creds must land in ubuntu's $HOME
#   ~/claude-pr-review-bot/bin/bootstrap.sh
#
# On first run it writes ~/.claude-pr-bot/.env, prompting for the two per-person values, and
# tells you to paste your two secrets. Then you re-run it and it finishes. See docs/SETUP.md.
#
# Everything lives under ~/.claude-pr-bot, which is outside the rsync destination
# (/var/local/cut-dry/current/), so `pnpm staging:dev`'s --delete can never touch it.
# The only file written outside $HOME is an additive Apache conf, config-tested first.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HOME/.claude-pr-bot"
BIN="$ROOT/bin"
REPO="${REPO:-GetCodifyAI/cut-and-dry}"
PORT="${PRBOT_PORT:-8899}"

# The box user that owns the bot. It must be the same account Claude Code is signed in as,
# because `claude` reads credentials from $HOME — ssm-user's shell drops you somewhere else.
PRBOT_USER="${PRBOT_USER:-ubuntu}"
[ "$(id -un)" = "$PRBOT_USER" ] \
  || { echo "Run as $PRBOT_USER (sudo su - $PRBOT_USER), not $(id -un)."; \
       echo "Override with PRBOT_USER=<name> if your box differs."; exit 1; }

echo "==> directories"
mkdir -p "$BIN" "$ROOT/wt" "$ROOT/state"
chmod 700 "$ROOT"
touch "$ROOT/seen" "$ROOT/used-nonces"
# Teammates' encrypted PATs + Slack IDs, written by the dashboard on sign-in.
[ -f "$ROOT/users.json" ] || echo '{}' > "$ROOT/users.json"
chmod 600 "$ROOT/users.json"

echo "==> .env"
# Ensure every documented key EXISTS rather than only writing the file when it is absent.
# A hand-made .env (secrets pasted in before the first bootstrap) previously ended up with no
# DRY_RUN line at all, so the code fell back to its "1" default and the dashboard stayed in
# dry run with no visible reason — sed found nothing to flip. Existing values are never
# overwritten, so flipping DRY_RUN to 0 survives a re-run.
touch "$ROOT/.env"; chmod 600 "$ROOT/.env"
ensure_key() { grep -q "^$1=" "$ROOT/.env" || printf '%s=%s\n' "$1" "$2" >> "$ROOT/.env"; }
# The two per-person values. Prompt when we have a terminal; otherwise write them empty and
# let require_env fail loudly later, which beats silently inheriting someone else's identity.
ask() {
  local key="$1" prompt="$2" default="${3:-}" val=""
  grep -q "^$key=.\+" "$ROOT/.env" && return 0
  if [ -t 0 ]; then read -rp "   $prompt${default:+ [$default]}: " val; fi
  printf '%s=%s\n' "$key" "${val:-$default}" >> "$ROOT/.env"
}
ensure_key GITHUB_PAT ""
ensure_key SLACK_WEBHOOK ""
ensure_key REPO "$REPO"
# Your GitHub login. The PAT must belong to this account — everything the dashboard posts is
# attributed to it, which is the whole point of the design.
ask REVIEWER "Your GitHub login"
# Your staging env slug: the branch name minus `-staging`. PUBLIC_URL and the Apache vhost
# hostname both derive from it, so there is one value to get right instead of two.
ask PRBOT_ENV "Your staging env slug (branch minus -staging, e.g. asela)"
ensure_key PRBOT_DOMAIN "staging.eng.cutanddry.com"
# GitHub login. Empty = token sign-in only. See docs/SETUP.md "GitHub login".
ensure_key GH_CLIENT_ID ""
ensure_key GH_CLIENT_SECRET ""
ensure_key GH_OAUTH_SCOPES ""
ensure_key DRY_RUN 1
ensure_key SKIP_BOT_PRS 0
ensure_key PRBOT_SECRET "$(openssl rand -hex 32)"
grep -q '^GITHUB_PAT=.\+' "$ROOT/.env" \
  || echo "   !! GITHUB_PAT is empty in $ROOT/.env — add it, then re-run"
chmod 600 "$ROOT/.env"

echo "==> gh"
if ! command -v gh >/dev/null; then
  v=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | jq -r .tag_name)
  curl -fsSL "https://github.com/cli/cli/releases/download/$v/gh_${v#v}_linux_arm64.tar.gz" \
    | tar -xz -C /tmp
  sudo install -m 0755 "/tmp/gh_${v#v}_linux_arm64/bin/gh" /usr/local/bin/gh
  echo "   installed gh $v (arm64)"
fi

echo "==> claude"
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash \
    || sudo npm install -g @anthropic-ai/claude-code
  echo "   installed claude — sign in AS $PRBOT_USER before the first review"
fi

echo "==> scripts"
# Bootstrap copies itself into $BIN, so it can also be re-run FROM $BIN. Installing a file
# onto itself makes `install` fail, and with `set -e` that aborted the run before the systemd
# and apache steps ever executed — silently skipping the parts you were re-running it for.
if [ "$SRC" != "$BIN" ]; then
  install -m 0755 "$SRC"/{pr-watch.sh,run-review.sh,bootstrap.sh} "$BIN/"
  install -m 0755 "$SRC/prbot-server.py" "$BIN/"
  install -m 0644 "$SRC"/prbot_diff.py "$BIN/"   # imported by the server, must sit beside it
  install -m 0644 "$SRC"/prbot_md.py "$BIN/"
  install -m 0644 "$SRC/lib-common.sh" "$BIN/"
else
  echo "   (running from $BIN — nothing to copy)"
fi

echo "==> base clone"
# shellcheck disable=SC1090
set -a; . "$ROOT/.env"; set +a
if [ -z "${GITHUB_PAT:-}" ]; then
  # The documented flow is: bootstrap (writes .env) -> you paste the secrets -> re-run.
  # Aborting here on the first run would strand that flow before systemd/apache/cron ever
  # got configured, so skip the clone and let the re-run pick it up.
  echo "   skipped — GITHUB_PAT is empty; re-run bootstrap once it is set"
elif [ ! -d "$ROOT/repo/.git" ]; then
  GH_TOKEN="$GITHUB_PAT" gh repo clone "$REPO" "$ROOT/repo" -- --filter=blob:none
fi
[ -d "$ROOT/repo/.git" ] && git -C "$ROOT/repo" config credential.helper \
  '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'

echo "==> pr-review skill"
mkdir -p "$HOME/.claude/skills"
# The skill ships in this repo. The agent runs with the PR worktree as its cwd, so a
# project-level copy would not be found — it has to be installed at the user level.
cp -r "$SRC/../skills/pr-review" "$HOME/.claude/skills/" 2>/dev/null \
  || echo "   !! skills/pr-review not found next to $SRC — reviews will run without it"

echo "==> systemd unit"
sudo tee /etc/systemd/system/prbot.service >/dev/null <<EOF
[Unit]
Description=PR review bot HTTP endpoint
After=network.target

[Service]
User=$PRBOT_USER
Environment=PRBOT_PORT=$PORT
# run-review.sh is spawned from this service and shells out to \`claude\`, which the native
# installer puts in ~/.local/bin — not on systemd's default PATH. Without this the agent
# step fails as "command not found" and surfaces only as an empty review.json.
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $BIN/prbot-server.py
# Only kill the server itself on stop. The default (control-group) reaps every process in
# the cgroup — including the detached run-review.sh a click spawned — so deploying while a
# review was running silently killed it and left the PR stuck reading "reviewing".
KillMode=process
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable prbot.service
# restart, not `enable --now`: the server reads .env once at startup, so an already-running
# instance would keep serving stale secrets and a stale DRY_RUN after you edit them.
sudo systemctl restart prbot.service

echo "==> apache proxy"
# The app's vhost lives in conf-enabled/cut-dry.conf and rewrites every unmatched path into
# /index.php/$1, and VirtualHosts do NOT inherit ProxyPass from the global server config —
# so a global snippet is silently ignored and /prbot 404s through PHP. Editing cut-dry.conf
# would work but CodeDeploy owns that file and would revert it.
#
# Instead: a dedicated name-based vhost on its own hostname. The nonprod staging nginx proxy
# maps `~^.*<env>\.staging\.eng\.cutanddry\.com$` to this box, so any prefix reaches us, and
# the ALB cert is *.staging.eng.cutanddry.com so TLS covers it. Nothing the app owns is
# touched, and the app stays reachable on app-<env>. Both live under /etc/apache2, outside
# the rsync target, so a staging sync cannot remove them.
# Derived from PRBOT_ENV in .env (sourced above), never hardcoded — see lib-common.sh.
PRBOT_HOST="${PRBOT_HOST:-prbot-${PRBOT_ENV:-unset}.${PRBOT_DOMAIN:-staging.eng.cutanddry.com}}"
[ "${PRBOT_ENV:-}" ] || { echo "   !! PRBOT_ENV is empty in $ROOT/.env — set it, then re-run"; exit 1; }
sudo a2enmod proxy proxy_http >/dev/null
sudo a2disconf prbot 2>/dev/null >/dev/null || true   # drop the old global-scope attempt
sudo rm -f /etc/apache2/conf-available/prbot.conf
sudo tee /etc/apache2/sites-available/prbot.conf >/dev/null <<EOF
<VirtualHost *:80>
    ServerName $PRBOT_HOST
    ProxyPreserveHost On
    ProxyPass        /prbot http://127.0.0.1:$PORT/prbot
    ProxyPassReverse /prbot http://127.0.0.1:$PORT/prbot
    # Nothing else is served on this hostname; the app answers on app-<env> as before.
    RedirectMatch 404 ^(?!/prbot)
    ErrorLog \${APACHE_LOG_DIR}/prbot-error.log
    CustomLog \${APACHE_LOG_DIR}/prbot-access.log combined
</VirtualHost>
EOF
sudo a2ensite prbot >/dev/null
if ! sudo apache2ctl configtest 2>&1 | grep -q "Syntax OK"; then
  sudo a2dissite prbot >/dev/null
  echo "   !! apache configtest FAILED — prbot vhost disabled, app untouched"; exit 1
fi
# Verify functionally, by asking the endpoint through the vhost. Do NOT parse `apache2ctl -M`
# to decide this: run as ubuntu it emits nothing useful, and that false negative previously
# triggered a needless full Apache restart (a real blip for the staging app) plus a spurious
# abort. A graceful reload is enough to pick up new LoadModule lines in practice; the restart
# stays only as a genuine fallback.
prbot_reachable() {
  [ "$(curl -s -m 5 -H "Host: $PRBOT_HOST" http://127.0.0.1/prbot/health 2>/dev/null)" = "ok" ]
}
sudo systemctl reload apache2
sleep 1
if ! prbot_reachable; then
  echo "   endpoint not reachable after reload — restarting apache"
  sudo systemctl restart apache2
  sleep 2
fi
if prbot_reachable; then
  echo "   apache ok (/prbot answers on $PRBOT_HOST)"
else
  sudo a2dissite prbot >/dev/null && sudo systemctl reload apache2
  echo "   !! /prbot unreachable — vhost disabled, app untouched"; exit 1
fi

echo "==> cron"
# Strip BOTH of our lines before re-adding, or each bootstrap run leaves another PATH=
# line behind. The PATH line cannot carry a trailing marker comment — cron would read the
# comment as part of the value — so it is matched literally instead.
cron_path="PATH=/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"
tmp=$(mktemp)
crontab -l 2>/dev/null | grep -vF "$cron_path" | grep -v 'pr-watch.sh' > "$tmp" || true
{
  echo "$cron_path"
  echo "*/3 * * * * flock -n /tmp/pr-watch.lock $BIN/pr-watch.sh >> $ROOT/watch.log 2>&1"
} >> "$tmp"
crontab "$tmp"; rm -f "$tmp"

echo
echo "Done. Checks:"
echo "  curl -s localhost:$PORT/prbot/health          # -> ok"
echo "  curl -s https://$PRBOT_HOST/prbot/health       # -> ok (through the ALB)"
echo "  $BIN/pr-watch.sh                              # -> Slack card per open request"
echo
echo "Sign in (you and every teammate):  https://$PRBOT_HOST/prbot/login"
echo "  Each person pastes their own GitHub PAT (repo scope) + Slack member ID once."
echo "  Reviews are shared per PR; posting and approving happen as each signed-in user."
echo
# Report the ACTUAL value, never a hardcoded assumption — a stale "nothing is written"
# reassurance is worse than none once someone has flipped it.
if grep -q '^DRY_RUN=0' "$ROOT/.env"; then
  echo "DRY_RUN=0 — the dashboard POSTS AND APPROVES ON GITHUB for real, as $(grep '^REVIEWER=' "$ROOT/.env" | cut -d= -f2)."
else
  echo "DRY_RUN=1 — reviews run and Slack reports them, but GitHub is never written."
fi
