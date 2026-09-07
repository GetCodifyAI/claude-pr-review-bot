#!/usr/bin/env python3
"""prbot-server.py — review dashboard, served on 127.0.0.1 behind Apache's /prbot proxy.

Reachable over the PUBLIC internet (the staging ALB answers *.staging.eng.cutanddry.com with
no auth in front). Pages need a signed session cookie, obtained by signing in with your own
GitHub PAT. The mutating actions embedded in a page — posting comments, approving — carry
their own 30-minute HMAC tokens minted at render time, so a forwarded or bookmarked page
cannot approve anything later, and a cross-site form post has no token to present.

Multi-user model: the REVIEW is per PR and shared (one agent run serves every reviewer);
SELECTION, POSTING and APPROVAL are per user, done with that user's own PAT so GitHub
attributes them to the human. Per-user markers live in state/<pr>/users/<login>/.

Routes
  GET  /prbot/health
  GET  /prbot/login  POST /prbot/login  sign in with a GitHub PAT (+ optional Slack member ID)
  GET  /prbot/logout
  GET  /prbot/settings  POST /prbot/settings
  GET  /prbot/?tab&sort                index — PRs awaiting YOUR review, with your state
  GET  /prbot/pr?pr=N                  detail — shared review, your editable findings, actions
  GET  /prbot/review?pr=N&exp&sig      start a review, then redirect to the detail page
  POST /prbot/post                     post the selected (possibly edited) comments as you
  POST /prbot/approve                  approve as you
"""
import base64
import calendar
import fcntl
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from hashlib import sha256
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import prbot_assets
import prbot_diff
import prbot_howimg
import prbot_learn
import prbot_md

BRAND = "Robin"                    # product name shown beside the logo (see prbot_assets)
CLAUDE_ICON = ("<svg viewBox='0 0 24 24' width=18 height=18 fill=currentColor aria-hidden=true>"
               "<path d='M12 2c.3 3.1 1 4.9 2.2 6 1.1 1.2 2.9 1.9 6 2.2-3.1.3-4.9 1-6 2.2"
               "-1.2 1.1-1.9 2.9-2.2 6-.3-3.1-1-4.9-2.2-6C8.6 11.2 6.8 10.5 3.7 10.2"
               "c3.1-.3 4.9-1 6-2.2C10.9 6.9 11.6 5.1 12 2z'/></svg>")
GH_ICON = ("<svg viewBox='0 0 16 16' width=23 height=23 fill=currentColor><path d='M8 0C3.58 0 0 "
           "3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01."
           "37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 "
           "1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.8"
           "7.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 "
           "0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 "
           "0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55."
           "38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z'/></svg>")
SLACK_ICON = ("<svg viewBox='0 0 122.8 122.8' width=22 height=22>"
              "<path fill='#E01E5A' d='M25.8 77.6a12.9 12.9 0 1 1-12.9-12.9h12.9zM32.3 77.6a12.9 "
              "12.9 0 0 1 25.8 0v32.3a12.9 12.9 0 0 1-25.8 0z'/>"
              "<path fill='#36C5F0' d='M45.2 25.8a12.9 12.9 0 1 1 12.9-12.9v12.9zM45.2 32.3a12.9 "
              "12.9 0 0 1 0 25.8H12.9a12.9 12.9 0 0 1 0-25.8z'/>"
              "<path fill='#2EB67D' d='M97 45.2a12.9 12.9 0 1 1 12.9 12.9H97zM90.5 45.2a12.9 12.9 "
              "0 0 1-25.8 0V12.9a12.9 12.9 0 0 1 25.8 0z'/>"
              "<path fill='#ECB22E' d='M77.6 97a12.9 12.9 0 1 1-12.9 12.9V97zM77.6 90.5a12.9 12.9 "
              "0 0 1 0-25.8h32.3a12.9 12.9 0 0 1 0 25.8z'/></svg>")

ROOT = Path(os.environ.get("ROOT", Path.home() / ".claude-pr-bot"))
BIN = Path(__file__).resolve().parent
STATE = ROOT / "state"
QUEUE = ROOT / "queue.json"

PAGE_TTL = 7 * 24 * 3600
ACTION_TTL = 30 * 60

SEV_ORDER = {"blocker": 0, "should-fix": 1, "nit": 2, "question": 3}
SEV_LABEL = {"blocker": "blocker", "should-fix": "should fix", "nit": "nit",
             "question": "question"}


def load_env():
    env, f = {}, ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
SECRET = ENV.get("PRBOT_SECRET", "")
# The SERVICE token: reads (diffs, PR metadata, the poller's searches) and the base clone.
# Never used to post or approve — those use the signed-in user's own PAT, see user_pat().
PAT = ENV.get("GITHUB_PAT", "")
REPO = ENV.get("REPO", "GetCodifyAI/cut-and-dry")
# The box owner. Their legacy per-PR markers (state/<pr>/posted.json etc., from before the
# multi-user layout) are read as theirs, so history survives the upgrade.
REVIEWER = ENV.get("REVIEWER", "")
DRY_RUN = ENV.get("DRY_RUN", "1") == "1"
# Slack, for server-side alerts (e.g. confirming a review was force-stopped). Bot token + channel
# is preferred (same as the shell scripts); a webhook is the fallback.
SLACK_WEBHOOK = ENV.get("SLACK_WEBHOOK", "")
SLACK_BOT_TOKEN = ENV.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = ENV.get("SLACK_CHANNEL", "")


def slack_notify(text):
    """Best-effort plain-text Slack message from the server. Never raises — a failed notify must
    not break the action that triggered it."""
    try:
        if SLACK_BOT_TOKEN and SLACK_CHANNEL:
            req = Request("https://slack.com/api/chat.postMessage",
                          data=json.dumps({"channel": SLACK_CHANNEL, "text": text}).encode(),
                          headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                                   "Content-Type": "application/json; charset=utf-8"})
            urlopen(req, timeout=8).read()
        elif SLACK_WEBHOOK:
            req = Request(SLACK_WEBHOOK, data=json.dumps({"text": text}).encode(),
                          headers={"Content-Type": "application/json"})
            urlopen(req, timeout=8).read()
    except Exception:
        pass

USERS = ROOT / "users.json"
SESSION_TTL = 30 * 24 * 3600


# --- users ---------------------------------------------------------------------------------
# users.json: {login: {pat_enc, slack_id, name, added}}. PATs are AES-encrypted with a key
# derived from PRBOT_SECRET — derived, not stored, so rotating the secret also invalidates
# every stored PAT, which is the right outcome if it was rotated because it leaked. The
# shell scripts only ever read login + slack_id; they never see a PAT.
def _users_key():
    return sha256(f"{SECRET}:users".encode()).hexdigest()


def _openssl(mode, data):
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-a", "-A",
                        mode, "-pass", "env:PRBOT_KEY"], input=data, capture_output=True,
                       text=True, env={**os.environ, "PRBOT_KEY": _users_key()})
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "openssl failed").strip()[:200])
    return r.stdout.strip()


def enc(plain):
    return _openssl("-e", plain)


def dec(cipher):
    return _openssl("-d", cipher)


def load_users():
    if not USERS.exists():
        return {}
    try:
        return json.loads(USERS.read_text()) or {}
    except json.JSONDecodeError:
        return {}


def save_users(users):
    tmp = USERS.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(USERS)


# All writers to users.json live in this one server process (pr-watch only reads it), so a
# process-wide lock is enough to make the load → modify → save sequence atomic and keep two
# simultaneous sign-ins / settings saves from losing each other's update.
_users_lock = threading.Lock()


def modify_users(fn):
    """Serialized read-modify-write of users.json. fn(users) mutates the dict in place."""
    with _users_lock:
        users = load_users()
        fn(users)
        save_users(users)


def user_pat(login):
    """The token to act as `login`: their GitHub-login OAuth token if they signed in that way
    (refreshed here if it has expired), else the PAT they pasted."""
    u = load_users().get(login) or {}
    if u.get("gh_token_enc"):
        tok = oauth_fresh_token(login, u)
        if tok:
            return tok
    if u.get("pat_enc"):
        try:
            return dec(u["pat_enc"])
        except RuntimeError:
            return ""
    return ""


# --- github login (OAuth) --------------------------------------------------------------------
# Works with a GitHub App (recommended: 8-hour user tokens + refresh, permissions scoped to
# the app, comments show the user's avatar with the app's badge) or a classic OAuth App (set
# GH_OAUTH_SCOPES=repo; tokens then have no expiry and no refresh). Either way the token acts
# AS THE USER — the whole point — and nobody pastes anything.
GH_CLIENT_ID = ENV.get("GH_CLIENT_ID", "")
GH_CLIENT_SECRET = ENV.get("GH_CLIENT_SECRET", "")
GH_OAUTH_SCOPES = ENV.get("GH_OAUTH_SCOPES", "")
PUBLIC_URL = ENV.get("PUBLIC_URL") or (
    f"https://robin-{ENV.get('PRBOT_ENV', '')}.{ENV.get('PRBOT_DOMAIN', 'staging.eng.cutanddry.com')}")
OAUTH_ENABLED = bool(GH_CLIENT_ID and GH_CLIENT_SECRET)


def oauth_state(nxt):
    """Signed, 10-minute state carrying where to land afterwards. Rejects forged callbacks."""
    exp = int(time.time()) + 600
    payload = f"{exp}:{nxt}"
    sig = hmac.new(SECRET.encode(), f"oauth:{payload}".encode(), sha256).hexdigest()
    # Raw, not percent-encoded: urlencode() in oauth_authorize_url does that once. Encoding
    # here too made GitHub echo back a double-encoded state that never verified.
    return f"{sig}:{payload}"


def oauth_check_state(state):
    sig, _, payload = (state or "").partition(":")
    if not (sig and payload):
        return None
    if not hmac.compare_digest(
            hmac.new(SECRET.encode(), f"oauth:{payload}".encode(), sha256).hexdigest(), sig):
        return None
    exp, _, nxt = payload.partition(":")
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return nxt if nxt.startswith("/prbot/") else "/prbot/"


OAUTH_BLOCKED = ROOT / "oauth-blocked"


def oauth_blocked():
    """True after a GitHub sign-in that succeeded at GitHub but could not see the repo — the
    org has not approved the app yet. Cleared by the first sign-in that can. Lets the login
    page demote the GitHub button instead of walking every newcomer into the same error."""
    return OAUTH_BLOCKED.exists()


def oauth_authorize_url(nxt):
    q = {"client_id": GH_CLIENT_ID, "redirect_uri": f"{PUBLIC_URL}/prbot/oauth/callback",
         "state": oauth_state(nxt)}
    if GH_OAUTH_SCOPES:
        q["scope"] = GH_OAUTH_SCOPES
    return "https://github.com/login/oauth/authorize?" + urlencode(q)


def oauth_token_request(params):
    """POST to GitHub's token endpoint. Returns the JSON dict, or {} on any failure."""
    body = urlencode({"client_id": GH_CLIENT_ID, "client_secret": GH_CLIENT_SECRET,
                      **params}).encode()
    req = Request("https://github.com/login/oauth/access_token", data=body,
                  headers={"Accept": "application/json",
                           "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode() or "{}")
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) and d.get("access_token") else {}


def oauth_store(login, d, name, prev):
    """Persist a token response. Expiry is absolute; 0 means the token never expires."""
    now = int(time.time())
    u = dict(prev)
    u.update({
        "gh_token_enc": enc(d["access_token"]),
        "gh_exp": now + int(d["expires_in"]) if d.get("expires_in") else 0,
        "name": name or u.get("name", ""),
        "added": u.get("added") or now, "updated": now,
    })
    if d.get("refresh_token"):
        u["gh_refresh_enc"] = enc(d["refresh_token"])
        u["gh_refresh_exp"] = now + int(d.get("refresh_token_expires_in") or 0)
    modify_users(lambda users: users.__setitem__(login, u))


def oauth_fresh_token(login, u):
    """Decrypt the user's token; if it expires within a minute, refresh it first."""
    try:
        exp = int(u.get("gh_exp") or 0)
        if not exp or exp - 60 > time.time():
            return dec(u["gh_token_enc"])
        if not u.get("gh_refresh_enc"):
            return ""
        d = oauth_token_request({"grant_type": "refresh_token",
                                 "refresh_token": dec(u["gh_refresh_enc"])})
        if not d:
            return ""
        oauth_store(login, d, u.get("name", ""), u)
        return d["access_token"]
    except RuntimeError:
        return ""


# --- per-user Claude account ---------------------------------------------------------------------
# There is no third-party "sign in with Anthropic"; what exists is `claude setup-token`, which
# hands a subscription token to automation (Anthropic documents it for GitHub Actions). A user
# pastes theirs once; reviews THEY trigger then run with CLAUDE_CODE_OAUTH_TOKEN set to it,
# billed to their own plan. Verified on the box: the env var wins over the stored login (a bogus
# token 401s instead of silently falling back). No token = the shared runner, as before.
def user_claude_token(login):
    """The user's Claude access token, refreshed if it is within a minute of expiring."""
    u = load_users().get(login) or {}
    if not u.get("claude_token_enc"):
        return ""
    try:
        exp = int(u.get("claude_exp") or 0)
        if exp and exp - 60 <= time.time():
            return claude_refresh(login, u)          # expired: refresh (returns "" if it can't)
        return dec(u["claude_token_enc"])
    except RuntimeError:
        return ""


def verify_claude_token(tok):
    """(ok, message). One tiny haiku call — a bad token fails fast with a 401."""
    try:
        r = subprocess.run(["claude", "-p", "Reply with exactly: OK", "--max-turns", "1",
                            "--model", "haiku"], capture_output=True, text=True, timeout=75,
                           stdin=subprocess.DEVNULL,
                           env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": tok})
    except FileNotFoundError:
        return False, "claude is not installed on this box."
    except subprocess.TimeoutExpired:
        return False, "Claude did not answer within 75 seconds — try again."
    if r.returncode != 0:
        tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["unknown error"])[-1]
        return False, tail[:200]
    return True, ""


# --- "Connect Claude": direct OAuth (PKCE), the way nerve and Claude Code itself do it ---------
# The earlier approach drove `claude setup-token` in a pty and scraped its terminal; that fails
# because the CLI renders the token masked, so it never appears as plaintext to read. So we run
# the same OAuth 2.0 + PKCE exchange Claude Code performs: send the user to Claude's authorize
# page, they paste back the code Claude shows, we exchange it for an access+refresh token and
# store it encrypted. The OAuth client is Claude Code's own (this is the credential
# CLAUDE_CODE_OAUTH_TOKEN is meant to hold) and the token is only ever used to run `claude -p`;
# verify_claude_token makes one real call before we keep it, so a token the CLI would reject is
# caught at connect time. Authorize/redirect/scope captured from the live `claude setup-token`
# on this box; token endpoint per public Claude Code OAuth notes (api.anthropic.com mirrors
# console.anthropic.com without its Cloudflare challenge, which a server cannot clear).
CLAUDE_OAUTH_CLIENT = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_AUTHORIZE = "https://claude.com/cai/oauth/authorize"
CLAUDE_OAUTH_REDIRECT = "https://platform.claude.com/oauth/code/callback"
CLAUDE_OAUTH_SCOPE = "org:create_api_key user:profile user:inference"
CLAUDE_TOKEN_ENDPOINTS = ["https://api.anthropic.com/v1/oauth/token",
                          "https://console.anthropic.com/v1/oauth/token",
                          "https://platform.claude.com/v1/oauth/token"]
CLAUDE_PENDING = {}                # login -> {verifier, state, started}
CLAUDE_LOCK = threading.Lock()
CLAUDE_PENDING_TTL = 15 * 60


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def claude_connect_cancel(login):
    with CLAUDE_LOCK:
        CLAUDE_PENDING.pop(login, None)


def claude_connect_pending(login):
    """The in-flight connect for this user, or None. Reaps anything older than the TTL."""
    with CLAUDE_LOCK:
        for k, v in list(CLAUDE_PENDING.items()):
            if time.time() - v["started"] > CLAUDE_PENDING_TTL:
                CLAUDE_PENDING.pop(k)
        return CLAUDE_PENDING.get(login)


def claude_connect_start(login):
    """(url, error). Generates PKCE + state and returns Claude's authorize URL."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(32))
    url = CLAUDE_OAUTH_AUTHORIZE + "?" + urlencode({
        "code": "true", "response_type": "code", "client_id": CLAUDE_OAUTH_CLIENT,
        "redirect_uri": CLAUDE_OAUTH_REDIRECT, "scope": CLAUDE_OAUTH_SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    with CLAUDE_LOCK:
        CLAUDE_PENDING[login] = {"verifier": verifier, "state": state, "url": url,
                                 "started": time.time()}
    return url, None


def _token_exchange(payload):
    """(data, error). POST to the token endpoint, trying each host until one answers JSON.

    A JSON error body (bad/expired code) means the endpoint worked and the grant is the
    problem — return it, don't try the next host. An HTML/challenge body means the wrong or a
    blocked host — move on."""
    body = json.dumps(payload).encode()
    last = "no token endpoint answered"
    for host in CLAUDE_TOKEN_ENDPOINTS:
        req = Request(host, data=body, method="POST",
                      headers={"Content-Type": "application/json", "User-Agent": "anthropic",
                               "Accept": "application/json"})
        try:
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode() or "{}"), None
        except HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if raw.strip().startswith("{"):
                try:
                    j = json.loads(raw)
                    return None, (j.get("error_description") or j.get("error") or raw[:200])
                except ValueError:
                    return None, raw[:200]
            last = f"{e.code} from {host.split('/')[2]}"
        except (OSError, ValueError) as e:
            last = str(e)[:150]
    return None, last


def claude_connect_code(login, pasted):
    """(data, error). Exchanges the pasted code for a token response.

    Claude concatenates the code and state with '#'; accept "code#state", a bare code, or the
    whole redirect URL pasted from the address bar."""
    p = claude_connect_pending(login)
    if not p:
        return None, "That sign-in attempt expired — start again."
    val = pasted.strip()
    if val.startswith("http"):
        q = parse_qs(urlparse(val).query)
        val = (q.get("code") or [""])[0] + ("#" + q["state"][0] if q.get("state") else "")
    code, _, state = val.partition("#")
    if not code:
        return None, "That does not look like a code — copy the code Claude showed you."
    if state and state != p["state"]:
        return None, "That code is from a different sign-in — start over and use the newest link."
    data, err = _token_exchange({
        "grant_type": "authorization_code", "code": code, "code_verifier": p["verifier"],
        "client_id": CLAUDE_OAUTH_CLIENT, "redirect_uri": CLAUDE_OAUTH_REDIRECT,
        "state": state or p["state"]})
    if err:
        return None, err
    if not data.get("access_token"):
        return None, "Claude returned no access token — start over."
    claude_connect_cancel(login)
    return data, None


def claude_refresh(login, u):
    """Refresh a stored Claude token in place. Returns the fresh access token, or ''."""
    if not u.get("claude_refresh_enc"):
        return ""
    try:
        rt = dec(u["claude_refresh_enc"])
    except RuntimeError:
        return ""
    data, err = _token_exchange({"grant_type": "refresh_token",
                                 "client_id": CLAUDE_OAUTH_CLIENT, "refresh_token": rt})
    if err or not data.get("access_token"):
        return ""
    store_claude_token(login, data)
    return data["access_token"]


def store_claude_token(login, data):
    """Persist an access(+refresh) token response, encrypted, with an absolute expiry."""
    now = int(time.time())

    def apply(users):
        u = users.get(login) or {}
        u["claude_token_enc"] = enc(data["access_token"])
        u["claude_exp"] = now + int(data["expires_in"]) if data.get("expires_in") else 0
        u["claude_added"] = u.get("claude_added") or now
        u["updated"] = now
        if data.get("refresh_token"):
            u["claude_refresh_enc"] = enc(data["refresh_token"])
        users[login] = u
    modify_users(apply)


def claude_connected(login):
    """True if this user has connected their own Claude account. Reviews require this — nobody
    runs on the shared box account, so nobody burns someone else's subscription."""
    return bool((load_users().get(login) or {}).get("claude_token_enc"))


def review_env(login):
    """Environment for a review spawned by `login`: it runs on THEIR Claude account (required —
    see claude_connected). PRBOT_ACTOR is the clicker (drives skill choice)."""
    env = {**os.environ, "PRBOT_RUN_AS": "shared", "PRBOT_ACTOR": login or ""}
    tok = user_claude_token(login) if login else ""
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        env["PRBOT_RUN_AS"] = login
    return env


# --- per-user review skill -------------------------------------------------------------------
# A user can bring their own pr-review skill; reviews they start use it (its logic runs, but
# run-review.sh always appends our own output contract, so any skill still yields the review.json
# the dashboard needs). No skill => the global default on the box. run-review picks the file by
# PRBOT_ACTOR and records the skill id next to the review so learnings can score it.
SKILLS_DIR = ROOT / "skills"
GLOBAL_SKILL_PATH = SKILLS_DIR / "_global.md"   # the editable team default (maintained here)


def skill_path(login):
    """The file backing a skill target: a login for a personal skill, "global" for the team
    default. Personal skills are `<login>.md`; the team default is `_global.md`."""
    return GLOBAL_SKILL_PATH if login == "global" else SKILLS_DIR / f"{login}.md"


def user_skill_path(login):
    return skill_path(login)


def read_skill(login):
    p = skill_path(login)
    try:
        return p.read_text() if p.exists() else ""
    except OSError:
        return ""


def user_skill(login):
    return read_skill(login)


def save_skill(login, text):
    """Returns True if saved. An empty team default is refused (one person must not be able to
    blank the skill everyone shares); an empty personal skill clears it back to the team default."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    p = skill_path(login)
    if text.strip():
        p.write_text(text)
        return True
    if login == "global":
        return False                        # never blank the shared skill from an ordinary save
    p.unlink(missing_ok=True)               # empty personal skill => fall back to the team default
    return True


def restore_global_skill():
    """Revert the team default to the installed pr-review skill. Guarded behind a typed confirm
    in the UI because it discards the team's edits for everyone."""
    GLOBAL_SKILL_PATH.unlink(missing_ok=True)


def save_user_skill(login, text):
    save_skill(login, text)


# Quick-add house rules: a reviewer types a plain-English preference ("don't ask for a Jira link
# in code comments") and it's tidied into a bullet under a managed "## Team rules" section, kept
# last in the doc so appends are trivial. Robin reads the whole skill, so the rule just applies.
RULES_MARKER = "## Team rules"
RULES_INTRO = ("Rules added from the dashboard — apply these on every review "
               "(they override the general guidance above when they conflict):")


def tidy_rule(rule):
    r = re.sub(r"\s+", " ", (rule or "").strip())
    if not r:
        return ""
    r = r[0].upper() + r[1:]
    if r[-1] not in ".!?":
        r += "."
    return r


def add_skill_rule(text, rule):
    """Append one tidied rule to a skill's managed Team-rules section (created if absent)."""
    r = tidy_rule(rule)
    if not r:
        return text
    bullet = f"- {r}"
    if RULES_MARKER in text:
        return text.rstrip() + f"\n{bullet}\n"      # the section is kept last, so append at end
    base = text.rstrip()
    head = (base + "\n\n") if base else ""
    return f"{head}{RULES_MARKER}\n\n{RULES_INTRO}\n\n{bullet}\n"


# Which skill a user's reviews run with: their own, or the shared team default. A saved choice
# wins; with none, we default to "own" when they have a personal skill, else "team". The team
# default is never destructively resettable through this — see save_skill / do_skill.
def active_skill(login):
    p = SKILLS_DIR / f"{login}.use"
    try:
        v = p.read_text().strip()
        if v in ("own", "team"):
            return v
    except OSError:
        pass
    return "own" if read_skill(login) else "team"


def set_active_skill(login, choice):
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    (SKILLS_DIR / f"{login}.use").write_text("own" if choice == "own" else "team")


def effective_skill(login):
    """(choice, label) — which skill actually runs, resolving 'own' with no personal skill."""
    if active_skill(login) == "own" and read_skill(login):
        return "own", "your own skill"
    return "team", "the team default"


# --- review effort ---------------------------------------------------------------------------
# How deep a review goes. The dashboard auto-sizes from the diff and lets the reviewer override;
# run-review.sh maps the key to a timeout + a depth instruction. Order is low → high.
EFFORT = {
    "quick":    ("Quick",    "diff only · ~10 min", "fast pass over just the changed lines"),
    "standard": ("Standard", "changed files · ~25 min", "the changed files and their context"),
    "deep":     ("Deep",     "whole-repo trace · ~40 min",
                 "traces impact across the repo — best for risky or large PRs"),
}
EFFORT_ORDER = ["quick", "standard", "deep"]

# Model the reviewer can pick at trigger time. "" = the account's default (no --model passed).
# Keys are the aliases Claude Code's --model accepts; validated server-side so nothing arbitrary
# ever reaches the CLI.
MODELS = [("", "Default", "your Claude plan's default"),
          ("opus", "Opus", "most capable \u00b7 deepest review"),
          ("sonnet", "Sonnet", "balanced \u00b7 faster"),
          ("haiku", "Haiku", "fastest \u00b7 light PRs")]
MODEL_KEYS = {k for k, _, _ in MODELS}

# The depth instruction appended to the active skill's prompt for each level. All three run the
# SAME skill — only this text and the timeout differ. These are the defaults; a team can edit them
# on the Skills page (stored as _effort_<level>.md) and run-review.sh receives the chosen text.
EFFORT_DEPTH = {
    "quick": (
        "Effort level: QUICK. Look only at the diff and the files it directly changes. Report "
        "only clear correctness bugs, broken logic and obvious runtime failures. Skip style, "
        "speculative concerns and minor edge cases. Keep findings very few — this is a fast pass."),
    "standard": (
        "Effort level: STANDARD. Review the changed files and their immediate callers and "
        "context. Cover correctness, error handling, obvious edge cases and clear risks. If the PR "
        "touches the pricing path (engine request/response, the engine-vs-DB flags, OG/catalog "
        "price sync, or ordering-time price display), catalog/search, or per-DP branching, trace "
        "whether it could make a wrong/stale/missing/zero price or wrong catalog data reach a "
        "buyer. Keep findings focused and high-confidence; each names its user-facing impact and a "
        "concrete failing scenario."),
    "deep": (
        "Effort level: DEEP — do a thorough, Devin-style deep analysis. Work through this method "
        "before writing any finding, and report only what you can tie to concrete evidence (the "
        "diff, the code, review threads, commit history):\n"
        "1. Intent. Read the PR description and review threads; identify what the change is trying "
        "to do and which behaviours it touches (data flow/state, query/API logic, UI, business "
        "rules, error handling).\n"
        "2. Comprehensive impact search FIRST. Before judging any line, search the whole "
        "repository for every component the change affects — callers and dependents of changed "
        "functions/fields, shared models, GraphQL queries/mutations/fragments, and any per-DP or "
        "per-vendor branching. Build the full blast radius up front; never discover impacts "
        "reactively.\n"
        "3. Trace data flow end to end for each meaningful change (request → controller → library "
        "→ model → response; on the frontend document → cache → component).\n"
        "4. Cut+Dry domain gates. For any PR that could plausibly touch these flows, trace the "
        "flow explicitly and judge by flow impact, not keywords:\n"
        "   - Pricing → ordering price. Could this change what the pricing engine returns, whether "
        "it is called at all, or the price the buyer finally sees — even incidentally (a refactor "
        "that flips a flag, reorders a param into a pricing call, changes retry/error handling or "
        "caching around a price fetch, or restructures a per-customer price loop)? Walk five "
        "stages: (1) engine REQUEST — PricingServiceLib/V2, request mappers/validators, batch "
        "jobs; (2) RESPONSE handling — response mappers, price caching, fallbacks from engine "
        "prices to DB/price-level/estimate; (3) FLAGS & routing that decide engine-vs-DB — "
        "pricingEngineEnabled, fetchOGPricesFromPricingServiceEnabled, "
        "fetchSalesCostFromPricingServiceEnabled, dateBasedPricingEnabled, GateKeeper toggles, and "
        "any VerifiedVendor / SupplierPortalVendorData / IntegrationData setting read on the price "
        "path (a change that accidentally flips, defaults, bypasses or stops reading one of these "
        "is the canonical bug); (4) PERSISTENCE into what buyers order from — DP OG sync libs, "
        "UpdateOGPricesFromPricingService, LocationPriceSyncer, OrderGuideCreator/Updater, "
        "PricingServiceCallbacks (watch chunking / early continue-break / swallowed exceptions / "
        "transaction changes that update SOME customers' prices and not others); (5) DISPLAY & use "
        "at ordering time — DraftPricingLib, unit-price libs, EditOrderMutation re-pricing, "
        "ZeroPricedItemsRule, order-UI price fields. Flag anything that could cause a WRONG, STALE, "
        "MISSING or ZERO price to reach a buyer, naming the stage and the concrete buyer-visible "
        "failure. NOT in scope on their own: order minimums/soft-min fees, delivery/fuel/pickup "
        "surcharges, taxes, transaction fees, rebates/promotions back-office — unless the change "
        "also alters an engine-derived item price.\n"
        "   - Catalog & search. Catalog data the engine prices against, ElasticSearch/OpenSearch "
        "index changes, category/product-model changes, catalog-service integration paths.\n"
        "   - DP-specific. Any branching on dpCode/vendorId/integrator type, SyncLibs, per-DP "
        "config/cutoff/pricing rules. Most DPs are pricing-engine integrated — a change 'only' to "
        "the non-integrated path still matters, and vice versa.\n"
        "5. Then examine, reporting only genuine issues: correctness & logic (conditionals, "
        "short-circuits, off-by-one/boundaries, skip-conditions that exclude valid states, "
        "null/undefined and defaults for new fields); error handling (failure modes covered, "
        "errors surfaced not swallowed, loading/empty states, missing try/catch on critical "
        "paths); concurrency & races; edge cases ONLY where the PR changes their handling (empty "
        "collections, single-vs-many, first-time/no-data, migration of old data under new code, "
        "network failure); performance & scalability (N+1 queries, unbounded loops/allocations, "
        "hot-path cost); security (input validation, authz gaps, injection, secrets); GraPhP model "
        "property renames on persisted nodes; Apollo cache correctness.\n"
        "6. Finding quality. Give each finding an honest confidence and keep only high-signal "
        "ones. Every finding must state the concrete user-facing impact and a specific failing "
        "scenario (the exact inputs/state that produce the wrong output or crash) — no vague or "
        "speculative findings. Cover the ground exhaustively in your analysis prose, but do not "
        "pad the findings list.\n"
        "7. Quality gates (note, don't block): if the PR adds, removes, renames or changes the "
        "default of a feature flag (GateKeeper etc.) without a matching update to "
        "docs/features/README.md, call it out with the flag name and what to document; if it adds "
        "meaningful new logic with no tests, name the key functions/hooks that lack coverage.\n"
        "Take the time the 40-minute budget allows. This is analysis depth only — you still write "
        "findings for a human to review and post, and you never post to GitHub yourself."),
}


def effort_depth_path(level):
    return SKILLS_DIR / f"_effort_{level}.md"


def effort_depth(level):
    """The depth instruction for a level: the team-edited text if present, else the default."""
    try:
        t = effort_depth_path(level).read_text()
        if t.strip():
            return t
    except OSError:
        pass
    return EFFORT_DEPTH.get(level, EFFORT_DEPTH["standard"])


def effort_edited(level):
    return effort_depth_path(level).exists()


def autosize_effort(meta):
    """Suggest an effort level from PR size (files + lines changed)."""
    files = meta.get("changedFiles") or 0
    lines = (meta.get("additions") or 0) + (meta.get("deletions") or 0)
    if files <= 3 and lines <= 40:
        return "quick"
    if files > 15 or lines > 500:
        return "deep"
    return "standard"


def review_effort(pr):
    """The effort a review actually ran at, or "" if unknown/never run."""
    try:
        v = (STATE / str(pr) / "effort").read_text().strip()
        return v if v in EFFORT else ""
    except OSError:
        return ""


def review_usage(pr):
    """Token usage + model recorded for the last review, or None. Written by run-review.sh from
    Claude's stream-json output; best-effort, so a missing/garbled file just means no usage line."""
    f = STATE / str(pr) / "usage.json"
    if not f.exists():
        return None
    try:
        u = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    inp = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
        + int(u.get("cache_creation_input_tokens") or 0)
    return {"model": u.get("model") or "unknown",
            "inputTokens": inp,
            "outputTokens": int(u.get("output_tokens") or 0),
            "totalTokens": inp + int(u.get("output_tokens") or 0),
            "costUsd": float(u.get("cost_usd") or 0)}


def review_risk(pr):
    """Domain-risk flags recorded by run-review.sh (pricing / catalog / dp), as a list."""
    try:
        return [f for f in (STATE / str(pr) / "risk").read_text().split() if f in RISK_INFO]
    except OSError:
        return []


RISK_INFO = {
    "pricing": ("💲", "Touches pricing-related paths",
                "double-check any price, cost, discount, rebate, margin or fee math, and "
                "consider looping in the pricing owner."),
    "catalog": ("📦", "Touches catalog / product paths",
                "verify catalog and search behavior — product models, ElasticSearch indexing, "
                "category assignment."),
    "dp": ("🔌", "Touches DP / integration paths",
           "check per-DP and per-vendor branching, sync libraries and cutoff/order logic."),
}

# Files that describe one review run — copied into history/<ts>/ when a re-run replaces it.
RUN_FILES = ("review.json", "effort", "focus", "skill", "runner", "head", "status")


def review_focus(pr):
    try:
        return (STATE / str(pr) / "focus").read_text().strip()
    except OSError:
        return ""


def archive_review(pr):
    """Move the current review into history/<ts>/ so a re-run doesn't lose it. No-op if none."""
    d = STATE / str(pr)
    if not (d / "review.json").exists():
        return
    h = d / "history" / str(int(time.time()))
    h.mkdir(parents=True, exist_ok=True)
    for name in RUN_FILES:
        f = d / name
        if f.exists():
            try:
                shutil.copy2(f, h / name)
            except OSError:
                pass
    # Clear the live review so the re-run starts clean; the archived copy is the history entry.
    (d / "review.json").unlink(missing_ok=True)


def review_history(pr):
    """Past runs, newest first: list of (ts, effort, focus, findings, event)."""
    hd = STATE / str(pr) / "history"
    if not hd.is_dir():
        return []
    out = []
    for sub in sorted(hd.iterdir(), reverse=True):
        if not sub.is_dir() or not sub.name.isdigit():
            continue
        rev = {}
        try:
            rev = json.loads((sub / "review.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        read = lambda n: (sub / n).read_text().strip() if (sub / n).exists() else ""  # noqa: E731
        out.append({"ts": int(sub.name), "effort": read("effort"), "focus": read("focus"),
                    "findings": len(rev.get("comments", [])),
                    "event": rev.get("event", "COMMENT")})
    return out


def load_history_review(pr, ts):
    f = STATE / str(pr) / "history" / str(ts) / "review.json"
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except json.JSONDecodeError:
        return None


def _kill_group(pidfile):
    """Force-stop a spawned job by its process group. SIGTERM then SIGKILL, because `claude -p`
    traps SIGTERM and keeps running (and keeps the per-PR flock held) — which is exactly why a
    stopped review used to be un-rerunnable: the lock never released, so is_running() stayed True.
    SIGKILL guarantees the group dies and the lock frees. Returns True if the group is confirmed
    gone, False if something is somehow still alive, None if there was no pid to kill."""
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        pidfile.unlink(missing_ok=True)
        return None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            break                                   # group already gone
        time.sleep(0.3)
    try:
        os.killpg(pid, 0)                            # signal 0 = liveness probe
        alive = True
    except OSError:
        alive = False
    pidfile.unlink(missing_ok=True)
    return not alive


def _notify_stopped(pr, user, confirmed, runner, kind="review"):
    """Slack confirmation that a force-stop actually halted the agent (so no Claude tokens keep
    burning unnoticed) — or a warning if it may not have."""
    meta = pr_meta(pr)[0]
    title, url = meta.get("title", f"PR #{pr}"), meta.get("url", ghurl_of(pr))
    sid = (load_users().get(user) or {}).get("slack_id") if user else ""
    by = f"<@{sid}>" if sid else (f"`@{user}`" if user else "someone")
    acct = (f" It was running on `{runner}`'s Claude account." if runner and runner != "shared"
            else " It was running on the shared box account." if runner else "")
    if confirmed:
        text = (f"🛑 {kind.capitalize()} of *<{url}|#{pr} — {title}>* was stopped by {by}. "
                f"✅ Confirmed the agent is gone and the lock is released — Claude usage has "
                f"halted.{acct}")
    else:
        text = (f"⚠️ Stop requested for the {kind} of *<{url}|#{pr} — {title}>* by {by}, but a "
                f"process may still be running on the box — please check that Claude usage "
                f"stopped.{acct}")
    slack_notify(text)


def stop_review(pr, user=""):
    """Force-stop a running review, verify it actually died, alert Slack, leave a re-runnable
    'stopped' status."""
    d = STATE / str(pr)
    runner = (d / "runner").read_text().strip() if (d / "runner").exists() else ""
    dead = _kill_group(d / "pid")
    time.sleep(0.2)
    (d / "status").write_text("stopped")
    confirmed = (dead is not False) and not is_running(pr)
    _notify_stopped(pr, user, confirmed, runner, "review")
    return confirmed


# --- QA guides -------------------------------------------------------------------------------
# A QA guide is a separate, lighter job than a review: run the pr-qa-guide skill against a PR and
# park the resulting markdown so it can be rendered and handed to QA. Its state keys are all
# `qa.*` so a guide and a review can coexist for the same PR without colliding.
def qa_running(pr):
    f = STATE / str(pr) / ".qa.lock"
    if not f.exists():
        return False
    try:
        fd = os.open(f, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def qa_status_text(pr):
    try:
        return (STATE / str(pr) / "qa.status").read_text().strip()
    except OSError:
        return ""


def load_qa(pr):
    try:
        return (STATE / str(pr) / "qa.md").read_text()
    except OSError:
        return ""


def qa_meta(pr):
    for name in ("qa_meta.json", "meta.json"):
        f = STATE / str(pr) / name
        if f.exists():
            try:
                return json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                pass
    return {}


def qa_state(pr):
    """'running' | 'failed' | 'stopped' | 'done' | 'none'."""
    if qa_running(pr):
        return "running"
    s = qa_status_text(pr)
    if s.startswith("failed"):
        return "failed"
    if s == "stopped" and not load_qa(pr):
        return "stopped"
    return "done" if load_qa(pr) else "none"


def qa_list():
    out = []
    if STATE.is_dir():
        for d in STATE.iterdir():
            if d.is_dir() and d.name.isdigit() and (d / "qa.md").exists():
                m = qa_meta(d.name)
                out.append({"num": d.name, "title": m.get("title", f"PR #{d.name}"),
                            "at": int((d / "qa.md").stat().st_mtime)})
    return sorted(out, key=lambda x: -x["at"])


def stop_qa(pr, user=""):
    d = STATE / str(pr)
    dead = _kill_group(d / "qa.pid")
    time.sleep(0.2)
    (d / "qa.status").write_text("stopped")
    confirmed = (dead is not False) and not qa_running(pr)
    _notify_stopped(pr, user, confirmed, "", "QA guide")
    return confirmed


def render_qa(md):
    """Render the QA guide markdown, turning '- [ ]' / '- [x]' items into real checkboxes."""
    h = prbot_md.render(md)
    h = re.sub(r"<li>\s*\[[ ]\]\s*", "<li class=task>", h)
    h = re.sub(r"<li>\s*\[[xX]\]\s*", "<li class='task done'>", h)
    return h


def verify_pat(pat):
    """(login, name, error). Proves the token is real and can see the repo before storing it."""
    r = gh(["api", "user"], token=pat, timeout=20)
    if r.returncode != 0:
        return None, None, "GitHub did not accept that token."
    try:
        me = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None, None, "Could not parse GitHub's response."
    login = me.get("login")
    if not login:
        return None, None, "GitHub returned no login for that token."
    if gh(["api", f"repos/{REPO}"], token=pat, timeout=20).returncode != 0:
        return None, None, f"That token cannot see {REPO} — it needs the `repo` scope."
    return login, me.get("name") or "", None


# --- sessions ------------------------------------------------------------------------------
def session_sig(login, exp):
    return hmac.new(SECRET.encode(), f"session:{login}:{exp}".encode(), sha256).hexdigest()


PRBOT_DOMAIN = ENV.get("PRBOT_DOMAIN", "staging.eng.cutanddry.com")


def _cookie_domain(host):
    """Scope the session cookie to the shared parent domain when we're on a real staging host, so
    one login works across robin-<env> and prbot-<env> (same box, same secret). Host-only on
    localhost/127.0.0.1 (tests) — a Domain that doesn't match the host is dropped by the browser."""
    h = (host or "").split(":")[0]
    if PRBOT_DOMAIN and (h == PRBOT_DOMAIN or h.endswith("." + PRBOT_DOMAIN)):
        return f"Domain=.{PRBOT_DOMAIN}; "
    return ""


def session_cookie(login, host=""):
    exp = int(time.time()) + SESSION_TTL
    return (f"prbot_s={login}:{exp}:{session_sig(login, exp)}; {_cookie_domain(host)}Path=/prbot; "
            f"Max-Age={SESSION_TTL}; HttpOnly; Secure; SameSite=Lax")


def clear_session_cookie(host=""):
    return (f"prbot_s=; {_cookie_domain(host)}Path=/prbot; Max-Age=0; HttpOnly; Secure; "
            "SameSite=Lax")


def _is_alias_host(host):
    """A robin-<env> / prbot-<env> host on our staging domain — the two aliases of this one box."""
    h = (host or "").split(":")[0]
    return (h.startswith("robin-") or h.startswith("prbot-")) and h.endswith("." + PRBOT_DOMAIN)


def _sibling_host(host):
    """The other alias of this box (robin- <-> prbot-), or "" when not on an alias host."""
    h = (host or "").split(":")[0]
    if not _is_alias_host(h):
        return ""
    return ("prbot-" + h[len("robin-"):]) if h.startswith("robin-") else ("robin-" + h[len("prbot-"):])


def _accept_url_ok(url):
    """A handoff token may only be handed to our own alias hosts' accept endpoint — never an
    arbitrary URL (that would leak the token)."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return (u.scheme == "https" and _is_alias_host(u.netloc)
            and u.path.rstrip("/") == "/prbot/handoff/accept")


def session_user(headers):
    """The signed-in login, or None. A user removed from users.json is signed out at once."""
    jar = SimpleCookie(headers.get("Cookie", ""))
    m = jar.get("prbot_s")
    if not m:
        return None
    parts = m.value.split(":")
    if len(parts) != 3:
        return None
    login, exp, sig = parts
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    if not hmac.compare_digest(session_sig(login, exp), sig):
        return None
    return login if login in load_users() else None


# --- signing ------------------------------------------------------------------------------
def sign(action, pr, exp):
    return hmac.new(SECRET.encode(), f"{action}:{pr}:{exp}".encode(), sha256).hexdigest()


def mint(action, pr, ttl):
    exp = int(time.time()) + ttl
    return exp, sign(action, pr, exp)


def link(action, pr, ttl=PAGE_TTL):
    # Pages are gated by the session cookie, so they get plain, bookmarkable URLs. Only the
    # actions that change something carry a signed, expiring token.
    if not action:
        return "/prbot/"
    if action == "pr":
        return f"/prbot/pr?pr={pr}"
    exp, sig = mint(action, pr, ttl)
    q = f"?pr={pr}&exp={exp}&sig={sig}" if pr else f"?exp={exp}&sig={sig}"
    return f"/prbot/{action}{q}"


def verify(action, pr, exp, sig):
    if not (SECRET and sig and exp):
        return "Missing or unsigned link."
    try:
        if int(exp) < time.time():
            return "This link has expired — reload the page for a fresh one."
    except ValueError:
        return "Malformed link."
    if not hmac.compare_digest(sign(action, pr, exp), sig):
        # Overwhelmingly this is a link minted under a previous PRBOT_SECRET — the box was
        # rebuilt, or .env was regenerated. Say so, rather than implying tampering.
        return ("This link was signed with a different key — it is almost certainly from "
                "before this box was rebuilt. Open the newest dashboard link in Slack.")
    return None


# --- github -------------------------------------------------------------------------------
def gh(args, timeout=45, token=None):
    """Runs gh with the service token, or with a specific user's PAT for writes-as-them."""
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "GH_TOKEN": token or PAT})


def gh_json(args, default=None):
    r = gh(args)
    if r.returncode != 0:
        return default
    try:
        return json.loads(r.stdout or "null")
    except json.JSONDecodeError:
        return default


def fetch_pr_files(pr):
    """(files, error). Never conflate a failed API call with an empty diff.

    `--slurp` wraps whatever came back in an array, so a GitHub error object arrives looking
    like a page of results — flattening it yields no filenames and every comment then looks
    un-anchorable. Left unchecked that posts a review with all findings dumped into the
    summary instead of anchored inline. So: validate the shape and refuse on anything odd.
    """
    last = "unknown error"
    for attempt in range(2):   # GitHub's files endpoint 404s intermittently under degradation
        r = gh(["api", f"repos/{REPO}/pulls/{pr}/files", "--paginate", "--slurp"])
        if r.returncode == 0:
            try:
                pages = json.loads(r.stdout or "null") or []
            except json.JSONDecodeError:
                last = "could not parse the GitHub response"
                continue
            files, bad = [], False
            for page in pages:
                if isinstance(page, list):
                    files.extend(page)
                elif isinstance(page, dict) and "filename" in page:
                    files.append(page)
                else:
                    bad, last = True, f"unexpected response: {str(page)[:200]}"
                    break
            if not bad:
                return files, None
        else:
            last = (r.stderr or "gh failed").strip().splitlines()[-1][:300]
        if attempt == 0:
            time.sleep(1.5)
    return None, last


def gist(body, limit=120):
    """One-line plain-text gist of a finding, for the approval checklist."""
    t = re.sub(r"```.*?```", "", body or "", flags=re.S)
    t = re.sub(r"[`*_>#]", "", t).replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    cut = t.split(". ")[0].strip(" .")
    return (cut[:limit].rstrip() + "…") if len(cut) > limit else cut


def default_approve_msg(rev):
    """LGTM plus the outstanding items, so approving still leaves a clear ask."""
    items = [c for c in rev.get("comments", [])
             if c.get("severity") in ("blocker", "should-fix")]
    if not items:
        return "LGTM 🚀"
    lines = ["LGTM — just handle these before merge:", ""]
    for c in items:
        loc = c.get("path", "")
        line = c.get("line")
        where = f"`{loc}:{line}`" if line else f"`{loc}`"
        lines.append(f"- {where} — {gist(c.get('body', ''))}")
    return "\n".join(lines)


def can_approve(pr, login):
    """(ok, why) — may `login` approve this PR?

    Deliberately NOT "is `login` a requested reviewer": GitHub clears the review request the
    moment any review is submitted, including a plain comment one. Gating on that made
    post-then-approve structurally impossible. What actually matters is that the PR is open,
    it is not the user's own PR (GitHub forbids self-approval), and this box genuinely
    reviewed it — which, combined with the signed session and action token, is the control.
    """
    r = gh(["api", f"repos/{REPO}/pulls/{pr}"], token=user_pat(login))
    if r.returncode != 0:
        err = (r.stderr or "unknown error").strip().splitlines()[-1][:250]
        return False, f"GitHub rejected the check: {err}"
    try:
        d = json.loads(r.stdout or "null") or {}
    except json.JSONDecodeError:
        return False, "Could not parse GitHub's response."
    if (d.get("state") or "").lower() != "open":
        return False, "That PR is no longer open."
    if d.get("draft"):
        return False, "That PR is still a draft."
    if ((d.get("user") or {}).get("login")) == login:
        return False, "GitHub does not allow approving your own PR."
    if not (STATE / str(pr) / "review.json").exists():
        return False, "No review has been run for this PR on this box."
    return True, ""
STATIC_DIR = BIN / "static"


def index_html():
    """The minimal HTML shell the React SPA mounts into (bundle built to bin/static/app.*)."""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(BRAND)}</title>"
        f"<link rel=icon href='{prbot_assets.FAVICON}'>"
        "<link rel=preconnect href='https://fonts.googleapis.com'>"
        "<link rel=preconnect href='https://fonts.gstatic.com' crossorigin>"
        "<link rel=stylesheet href='https://fonts.googleapis.com/css2?"
        "family=Inter:wght@400;500;600;700&display=swap'>"
        "<link rel=stylesheet href='/prbot/static/app.css'>"
        "</head><body><div id=root></div>"
        "<script src='/prbot/static/app.js'></script></body></html>")


# --- state --------------------------------------------------------------------------------
def is_running(pr):
    """True while run-review.sh holds the per-PR flock.

    Exact, unlike guessing from a timestamp: if we can take the lock, nothing is running.
    A review killed mid-flight (systemd used to reap detached children on restart) otherwise
    leaves `status` reading "reviewing" forever.
    """
    f = STATE / str(pr) / ".lock"
    if not f.exists():
        return False
    try:
        fd = os.open(f, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def udir(pr, login):
    """Where one user's markers for one PR live. The review itself stays at STATE/<pr>/."""
    return STATE / str(pr) / "users" / login


def upath(pr, login, name):
    """Read path for a per-user marker.

    Falls back to the legacy per-PR marker for the box owner: before the multi-user layout
    every marker sat directly in STATE/<pr>/, and all of it was REVIEWER's. Writers always
    target udir(); only reads consult the legacy spot, so nothing new lands there.
    """
    p = udir(pr, login) / name
    if p.exists():
        return p
    legacy = STATE / str(pr) / name
    if login == REVIEWER and legacy.exists():
        return legacy
    return p


def touch_user(pr, login, name="opened"):
    d = udir(pr, login)
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    if not f.exists():
        f.write_text(str(int(time.time())))


def pr_state(pr, login):
    d = STATE / str(pr)
    if upath(pr, login, "archived").exists():
        return "archived"
    if upath(pr, login, "approved").exists():
        return "approved"
    if upath(pr, login, "posted.json").exists():
        return "posted"
    s = (d / "status").read_text().strip() if (d / "status").exists() else ""
    if not s:
        return "new"
    if s.startswith("failed"):
        return "failed"
    if s == "stopped":
        return "stopped"
    if s.startswith(("done", "posted", "dry-run")):
        return "done"
    return "reviewing" if is_running(pr) else "stalled"


def load_review(pr):
    f = STATE / str(pr) / "review.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return None


def queue():
    if QUEUE.exists():
        try:
            return json.loads(QUEUE.read_text())
        except json.JSONDecodeError:
            pass
    return []


def fetch_pr_meta(pr):
    """Fetch a PR's identity from GitHub for one that isn't in the local queue (e.g. opened by
    number/URL from the command palette). Normalized to the queue.json shape and cached to
    meta.json so the detail header shows the real title/author/size, not just the number."""
    d = gh_json(["pr", "view", str(pr), "--repo", REPO, "--json",
                 "number,title,url,additions,deletions,changedFiles,author,isDraft,"
                 "headRefOid,createdAt,updatedAt"], default=None)
    if not isinstance(d, dict) or not d.get("number"):
        return None
    m = {"number": d["number"], "title": d.get("title", ""), "url": d.get("url", ""),
         "additions": d.get("additions", 0), "deletions": d.get("deletions", 0),
         "changedFiles": d.get("changedFiles", 0),
         "author": (d.get("author") or {}).get("login", ""),
         "isDraft": d.get("isDraft", False), "head": d.get("headRefOid", ""),
         "createdAt": d.get("createdAt"), "updatedAt": d.get("updatedAt")}
    try:
        f = STATE / str(pr) / "meta.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(m))
    except OSError:
        pass
    return m


def pr_meta(pr):
    """Identity for a PR, from the live queue if still there, else the cached copy, else fetched
    live from GitHub for a PR opened by number that was never in this user's queue.
    """
    for item in queue():
        if str(item.get("number")) == str(pr):
            return item, True
    f = STATE / str(pr) / "meta.json"
    if f.exists():
        try:
            return json.loads(f.read_text()), False
        except json.JSONDecodeError:
            pass
    fetched = fetch_pr_meta(pr)
    if fetched:
        return fetched, False
    return {"number": pr, "title": f"PR #{pr}"}, False


def requested_of(item):
    """Logins a queue row is awaiting. Rows written before multi-user carry no `requested`
    and were, by construction, the owner's."""
    r = item.get("requested")
    return list(r) if isinstance(r, list) else [REVIEWER]


def mine(pr, login):
    """Has this user touched this PR here — opened, posted, approved or archived it?"""
    if udir(pr, login).exists():
        return True
    if login != REVIEWER:
        return False
    d = STATE / str(pr)
    return any((d / n).exists() for n in ("posted.json", "approved", "archived", "status"))


def all_prs(login):
    """The user's queue first, then anything they touched here that has since left it."""
    live = [i for i in queue() if login in requested_of(i)]
    seen = {str(i.get("number")) for i in live}
    extra = []
    if STATE.exists():
        for d in STATE.iterdir():
            if d.is_dir() and d.name.isdigit() and d.name not in seen and mine(d.name, login):
                extra.append((pr_meta(d.name)[0], False))
    extra.sort(key=lambda m: -int(m[0].get("number", 0)))
    return [(i, True) for i in live] + extra


def ghurl_of(pr):
    return (pr_meta(pr)[0].get("url") or f"https://github.com/{REPO}/pull/{pr}")


# --- stacked PRs -----------------------------------------------------------------------------
# A "stack" is a chain of open PRs where each one's base branch is the previous one's head branch
# (Graphite/ghstack style). We walk that chain from a given PR so a reviewer can review the whole
# stack from one click instead of hunting down each PR.
_SFIELDS = "number,title,baseRefName,headRefName,url"


def _pr_bh(pr):
    d = gh_json(["pr", "view", str(pr), "--repo", REPO, "--json", _SFIELDS], default=None)
    return d if isinstance(d, dict) and d.get("number") else None


def _pr_first(flag, branch):
    rows = gh_json(["pr", "list", "--repo", REPO, "--state", "open", flag, branch,
                    "--json", _SFIELDS, "--limit", "5"], default=[])
    return rows[0] if isinstance(rows, list) and rows else None


def pr_stack(pr):
    """Open PRs forming the stack that contains `pr`, ordered top (nearest mainline) → bottom.
    Just [pr] if it isn't stacked. A few gh calls, so call it on demand, not on every page."""
    info = _pr_bh(pr)
    if not info:
        return []
    chain, seen, cur = [info], {info["number"]}, info
    for _ in range(15):                          # up: a PR whose head == cur's base is the parent
        p = _pr_first("--head", cur["baseRefName"])
        if not p or p["number"] in seen:
            break
        chain.insert(0, p); seen.add(p["number"]); cur = p
    cur = info
    for _ in range(15):                          # down: a PR whose base == cur's head is the child
        c = _pr_first("--base", cur["headRefName"])
        if not c or c["number"] in seen:
            break
        chain.append(c); seen.add(c["number"]); cur = c
    return chain


def pr_reviewers(pr):
    """GitHub's reviewer list + each one's status + overall decision — like GitHub's Reviewers
    sidebar. Uses the REST API (works with a plain `repo` token; the GraphQL reviewRequests query
    needs `read:org`, which our service token doesn't have)."""
    reqs = gh_json(["api", f"repos/{REPO}/pulls/{pr}/requested_reviewers"], default={})
    # --slurp wraps each page in an array so --paginate stays valid JSON; flatten it.
    pages = gh_json(["api", f"repos/{REPO}/pulls/{pr}/reviews", "--paginate", "--slurp"], default=[])
    reviews = []
    for pg in pages if isinstance(pages, list) else []:
        reviews.extend(pg if isinstance(pg, list) else [pg])
    requested = []
    if isinstance(reqs, dict):
        for u in reqs.get("users") or []:
            if u.get("login"):
                requested.append(u["login"])
        for t in reqs.get("teams") or []:
            if t.get("slug") or t.get("name"):
                requested.append(t.get("slug") or t.get("name"))
    disp, eff = {}, {}                                # latest state per user (display) + effective
    if isinstance(reviews, list):
        for rv in reviews:                            # chronological
            login = (rv.get("user") or {}).get("login")
            st = rv.get("state")
            if not login or not st or st == "PENDING":
                continue
            disp[login] = st
            if st in ("APPROVED", "CHANGES_REQUESTED"):
                eff[login] = st
            elif st == "DISMISSED":
                eff.pop(login, None)
    out, seen = [], set()
    for who in requested:                             # a (re-)requested reviewer reads as pending
        out.append({"login": who, "state": "AWAITING"})
        seen.add(who)
    for login, st in disp.items():
        if login not in seen:
            out.append({"login": login, "state": st})
            seen.add(login)
    if any(s == "CHANGES_REQUESTED" for s in eff.values()):
        decision = "CHANGES_REQUESTED"
    elif any(s == "APPROVED" for s in eff.values()) and not requested:
        decision = "APPROVED"
    elif requested:
        decision = "REVIEW_REQUIRED"
    else:
        decision = None
    return {"reviewers": out, "decision": decision}


def sev_counts(comments):
    c = {}
    for x in comments:
        s = x.get("severity", "nit")
        c[s] = c.get(s, 0) + 1
    return c


# --- time ----------------------------------------------------------------------------------
def iso_ts(v):
    try:
        return calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def ago(ts):
    if not ts:
        return ""
    d = int(time.time()) - int(ts)
    if d < 90:
        return "just now"
    for n, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if d >= n:
            return f"{d // n}{unit} ago"
    return "just now"


def fmt_date(ts):
    return time.strftime("%m/%d/%y", time.localtime(int(ts))) if ts else ""


def marker(pr, name, login):
    """Read one user's state marker; plain-text (legacy) or JSON. Returns a dict."""
    f = upath(pr, login, name)
    if not f.exists():
        return {}
    raw = f.read_text().strip()
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {"at": int(raw or 0)}
    except (json.JSONDecodeError, ValueError):
        try:
            return {"at": int(raw)}
        except ValueError:
            return {"at": 0}


def pr_times(pr, login):
    """Every timestamp we know about a PR for this user, for sorting and display."""
    d = STATE / str(pr)
    rev_f = d / "review.json"
    return {
        "reviewed": int(rev_f.stat().st_mtime) if rev_f.exists() else 0,
        "posted": marker(pr, "posted.json", login).get("at", 0),
        "approved": marker(pr, "approved", login).get("at", 0),
    }


TABS = [("todo", "To review"), ("reviewed", "Reviewed"), ("posted", "Posted"),
        ("approved", "Approved"), ("archived", "Archived"), ("all", "All")]
TAB_DESC = {
    "todo": "PRs awaiting your review. Open one to run the agent, then post the findings "
            "worth keeping.",
    "reviewed": "The agent has finished — read the findings and post the ones you agree with. "
                "Nothing is on GitHub yet.",
    "posted": "You've posted comments on these. Approve when you're satisfied, or leave them "
              "for the author.",
    "approved": "Done — you approved these on GitHub.",
    "archived": "Hidden from your working set. Restore any of them anytime.",
    "all": "Everything you've touched, except archived.",
}


def tab_of(st):
    if st == "archived":
        return "archived"
    if st == "approved":
        return "approved"
    if st == "posted":
        return "posted"
    if st in ("done", "failed", "stalled", "stopped"):
        return "reviewed"
    return "todo"


# --- handler ------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "prbot"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def reply(self, code, body, ctype="text/html; charset=utf-8", cookie=None):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, to, cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def to_login(self):
        # Only ever bounce back inside /prbot — an open redirect otherwise.
        nxt = self.path if self.path.startswith("/prbot/") else "/prbot/"
        return self.redirect(f"/prbot/login?next={quote(nxt, safe='')}")

    # -- GET ---------------------------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        route = u.path.rstrip("/").removeprefix("/prbot") or "/"

        if route == "/health":
            return self.reply(200, "ok", "text/plain; charset=utf-8")
        if route.startswith("/static/"):
            return self.serve_static(route)
        if route.startswith("/api/"):
            return self.api_get(route, q)
        if route == "/logout":
            return self.redirect("/prbot/login",
                                 cookie=clear_session_cookie(self.headers.get("Host", "")))
        if route == "/oauth/start":
            if not OAUTH_ENABLED:
                return self.redirect("/prbot/login?err=" + quote(
                    "GitHub login isn't configured on this box \u2014 sign in with a token."))
            nxt = (q.get("next") or ["/prbot/"])[0]
            return self.redirect(oauth_authorize_url(nxt if nxt.startswith("/prbot/")
                                                     else "/prbot/"))
        if route == "/oauth/callback":
            return self.oauth_callback((q.get("code") or [""])[0],
                                       (q.get("state") or [""])[0],
                                       (q.get("error_description") or
                                        q.get("error") or [""])[0])
        # Everything else is a client-routed SPA page \u2192 serve the shell. React calls
        # /api/me and shows the login screen when there is no session.
        # --- cross-host SSO: carry an existing session between the robin-/prbot- aliases -------
        if route == "/handoff":
            # This host may already hold a session (e.g. prbot-). If authed, mint a short handoff
            # token and bounce to the sibling's accept endpoint; else bounce back so it shows login.
            nxt = (q.get("next") or [""])[0]
            if not _accept_url_ok(nxt):
                return self.redirect("/prbot/login")
            user = session_user(self.headers)
            if user:
                exp = int(time.time()) + 120
                sig = sign("handoff", user, exp)
                sep = "&" if "?" in nxt else "?"
                return self.redirect(f"{nxt}{sep}login={quote(user)}&exp={exp}&sig={sig}")
            return self.redirect(nxt)
        if route == "/handoff/accept":
            hlogin = (q.get("login") or [""])[0]
            hexp = (q.get("exp") or [""])[0]
            hsig = (q.get("sig") or [""])[0]
            ok = (hlogin and hexp.isdigit() and hlogin in load_users() and int(hexp) > time.time()
                  and hmac.compare_digest(sign("handoff", hlogin, int(hexp)), hsig))
            if ok:
                return self.redirect("/prbot/",
                                     cookie=session_cookie(hlogin, self.headers.get("Host", "")))
            return self.redirect("/prbot/?sso=1")
        host = self.headers.get("Host", "")
        sib = _sibling_host(host)
        if sib and not q.get("sso") and route != "/login" and not session_user(self.headers):
            accept = f"https://{host}/prbot/handoff/accept"
            return self.redirect(f"https://{sib}/prbot/handoff?next=" + quote(accept, safe=""))
        return self.reply(200, index_html())

    # -- POST --------------------------------------------------------------------------------
    # --- JSON API + static (React frontend) -------------------------------------------------
    def api_json(self, obj, status=200, cookie=None):
        return self.reply(status, json.dumps(obj), "application/json; charset=utf-8", cookie)

    def serve_static(self, route):
        name = route[len("/static/"):]
        ctype = ("application/javascript; charset=utf-8" if name.endswith(".js")
                 else "text/css; charset=utf-8" if name.endswith(".css")
                 else "application/octet-stream")
        f = STATIC_DIR / name
        # basic traversal guard + must sit under STATIC_DIR
        if ".." in name or not f.is_file() or STATIC_DIR not in f.resolve().parents:
            return self.reply(404, "not found", "text/plain; charset=utf-8")
        try:
            return self.reply(200, f.read_text(), ctype)
        except OSError:
            return self.reply(404, "not found", "text/plain; charset=utf-8")

    def api_get(self, route, q):
        user = session_user(self.headers)
        if route == "/api/me":
            return self.api_json(self.api_me(user))
        if not user:
            return self.api_json({"error": "unauthorized"}, 401)
        if route == "/api/queue":
            return self.api_json(self.api_queue(user, (q.get("tab") or ["todo"])[0],
                                                (q.get("sort") or ["newest"])[0]))
        if route == "/api/pr":
            pr = (q.get("pr") or [""])[0]
            if not pr.isdigit():
                return self.api_json({"error": "missing pr"}, 400)
            return self.api_json(self.api_pr(pr, user, (q.get("v") or [""])[0]))
        if route == "/api/qa":
            pr = (q.get("pr") or [""])[0]
            return self.api_json(self.api_qa_detail(pr, user) if pr.isdigit()
                                 else self.api_qa_index(user))
        if route == "/api/skills":
            return self.api_json(self.api_skills(user))
        if route == "/api/integrations":
            return self.api_json(self.api_integrations(user))
        if route == "/api/learnings":
            return self.api_json(self.api_learnings(user))
        if route == "/api/how":
            return self.api_json({"images": prbot_howimg.IMG, "brand": BRAND,
                                  "reviewer": REVIEWER, "tabs": [{"key": k, "label": lbl,
                                                                  "desc": TAB_DESC.get(k, "")}
                                                                 for k, lbl in TABS if k != "all"]})
        if route == "/api/stack":
            pr = (q.get("pr") or [""])[0]
            if not pr.isdigit():
                return self.api_json({"error": "missing pr"}, 400)
            return self.api_json(self.api_stack(pr, user))
        return self.api_json({"error": "not found"}, 404)

    def _run_form_data(self, user, meta):
        _, skill_label = effective_skill(user)
        return {"suggested": autosize_effort(meta),
                "levels": [{"key": k, "name": EFFORT[k][0], "sub": EFFORT[k][1]}
                           for k in EFFORT_ORDER],
                "models": [{"key": k, "name": n, "sub": sub} for k, n, sub in MODELS],
                "skillLabel": skill_label}

    def _tok(self, action, pr, ttl=ACTION_TTL):
        exp, sig = mint(action, pr, ttl)
        return {"exp": exp, "sig": sig}

    def api_pr(self, pr, user, version):
        touch_user(pr, user)
        if version.isdigit():
            rev = load_history_review(pr, int(version)) or {}
            comments = sorted(rev.get("comments", []),
                              key=lambda c: SEV_ORDER.get(c.get("severity"), 9))
            return {"historyView": True, "pr": pr, "ts": int(version),
                    "title": qa_meta(pr).get("title") or pr_meta(pr)[0].get("title", f"PR #{pr}"),
                    "when": f"{fmt_date(int(version))} ({ago(int(version))})",
                    "summary": rev.get("summary", ""),
                    "findings": [{"severity": c.get("severity", "nit"),
                                  "sevLabel": SEV_LABEL.get(c.get("severity", "nit"),
                                                            c.get("severity", "nit")),
                                  "path": c.get("path", "?"), "line": c.get("line", "?"),
                                  "body": c.get("body", "")} for c in comments]}
        st = pr_state(pr, user)
        meta, active = pr_meta(pr)
        eff = review_effort(pr)
        foc = review_focus(pr)
        runner = (STATE / pr / "runner").read_text().strip() if (STATE / pr / "runner").exists() else ""
        head_f = STATE / pr / "head"
        cur_head = meta.get("head", "")
        stale = bool(head_f.exists() and cur_head and head_f.read_text().strip() != cur_head)
        out = {
            "pr": pr, "title": meta.get("title", f"PR #{pr}"), "state": st,
            "ghUrl": meta.get("url", f"https://github.com/{REPO}/pull/{pr}"),
            "author": meta.get("author", ""),
            "size": (f"+{meta.get('additions', 0):,} −{meta.get('deletions', 0):,} · "
                     f"{meta['changedFiles']} files") if meta.get("changedFiles") else "",
            "dryRun": DRY_RUN,
            "awaiting": bool(active and user in requested_of(meta)),
            "runner": runner,
            "effortBadge": ({"label": EFFORT[eff][0], "hint": EFFORT[eff][2]}
                            if eff and st not in ("reviewing", "queued") else None),
            "usage": (review_usage(pr) if st not in ("reviewing", "queued") else None),
            "focus": foc,
            "stale": stale,
            "risk": [{"icon": RISK_INFO[f][0], "title": RISK_INFO[f][1], "note": RISK_INFO[f][2]}
                     for f in review_risk(pr)],
            "timeline": self._timeline_data(pr, user),
            "reviewers": (pr_reviewers(pr) if st not in ("reviewing", "queued") else None),
            "claudeConnected": claude_connected(user),
            "runForm": self._run_form_data(user, meta),
            "tokens": {"review": self._tok("review", pr, PAGE_TTL),
                       "stop": self._tok("stop", pr), "post": self._tok("post", pr),
                       "approve": self._tok("approve", pr), "markdone": self._tok("markdone", pr),
                       "archive": self._tok("archive", pr),
                       "unarchive": self._tok("unarchive", pr)},
            "history": review_history(pr),
        }
        if st == "reviewing":
            s = (STATE / pr / "status").read_text().strip().lower()
            reff = eff or "standard"
            out["reviewing"] = {
                "phases": ["Fetching the PR", "Checking out the branch", "Reviewing the diff",
                           "Writing the findings"],
                "cur": (0 if "fetch" in s else 1 if ("checking out" in s or "queued" in s)
                        else 2 if "reviewing" in s else 3),
                "queued": "queued" in s, "effortLabel": EFFORT[reff][0],
                "effortHint": EFFORT[reff][2], "focus": foc}
            return out
        if st == "stopped":
            out["stopped"] = {"halted": not is_running(pr)}
            return out
        if st == "stalled":
            log = STATE / pr / "agent.log"
            out["stalled"] = {"was": (STATE / pr / "status").read_text().strip(),
                              "tail": (log.read_text()[-400:].strip() if log.exists() else "")}
            return out
        rev = load_review(pr)
        appr = marker(pr, "approved", user)
        if not rev:
            out["notReviewed"] = True
            if st == "failed":
                out["failed"] = (STATE / pr / "status").read_text().strip()
            if appr.get("at"):
                out["approved"] = self._approved_data(appr, user)
            return out
        out["review"] = self._review_data(pr, user, rev, appr)
        out["showMarkDone"] = st != "approved"
        return out

    def _timeline_data(self, pr, user):
        t = pr_times(pr, user)
        posted = marker(pr, "posted.json", user)
        return [
            {"label": "Reviewed", "done": bool(t["reviewed"]),
             "note": ago(t["reviewed"]) if t["reviewed"] else ""},
            {"label": "Comments posted", "done": bool(t["posted"]),
             "note": (f"{posted.get('inline', 0)} inline · {fmt_date(t['posted'])}"
                      if t["posted"] else "")},
            {"label": "Approved", "done": bool(t["approved"]),
             "note": fmt_date(t["approved"]) if t["approved"] else ""}]

    def _approved_data(self, appr, user):
        return {"at": fmt_date(appr["at"]), "ago": ago(appr["at"]), "manual": bool(appr.get("manual")),
                "body": appr.get("body", ""), "user": user}

    def _review_data(self, pr, user, rev, appr):
        ev = rev.get("event", "COMMENT")
        comments = sorted(rev.get("comments", []),
                          key=lambda c: SEV_ORDER.get(c.get("severity"), 9))
        cs = sev_counts(comments)
        findings = []
        for i, c in enumerate(comments):
            findings.append({"i": i, "severity": c.get("severity", "nit"),
                             "sevLabel": SEV_LABEL.get(c.get("severity", "nit"),
                                                       c.get("severity", "nit")),
                             "path": c.get("path", "?"), "line": c.get("line", "?"),
                             "thread": (c["reply_to"] if c.get("reply_to") else None),
                             "body": c.get("body", ""), "suggestion": c.get("suggestion", "") or "",
                             "low": c.get("confidence") == "low"})
        data = {
            "event": ev, "summary": rev.get("summary", ""),
            "explainer": rev.get("explainer", ""), "analysis": rev.get("analysis", ""),
            "chips": [{"kind": k, "n": n, "label": SEV_LABEL.get(k, k)}
                      for k, n in sorted(cs.items(), key=lambda kv: SEV_ORDER.get(kv[0], 9))],
            "findings": findings, "count": len(comments),
            "posted": upath(pr, user, "posted.json").exists(),
            "postLabel": "Post selected" + (" (dry run)" if DRY_RUN else " to GitHub"),
        }
        if appr.get("at"):
            data["approved"] = self._approved_data(appr, user)
        else:
            blockers = cs.get("blocker", 0)
            data["approve"] = {"lgtm": blockers == 0 and ev != "REQUEST_CHANGES",
                               "blockers": blockers, "defaultMsg": default_approve_msg(rev)}
        return data

    def api_qa_index(self, user):
        return {"guides": [{"num": g["num"], "title": g["title"],
                            "when": f"{fmt_date(g['at'])} ({ago(g['at'])})"} for g in qa_list()]}

    def api_qa_detail(self, pr, user):
        st = qa_state(pr)
        meta = qa_meta(pr)
        out = {"pr": pr, "title": meta.get("title", f"PR #{pr}"),
               "ghUrl": meta.get("url", f"https://github.com/{REPO}/pull/{pr}"),
               "state": st, "connected": claude_connected(user),
               "genToken": self._tok("qa", pr, PAGE_TTL)}
        if st == "running":
            s = qa_status_text(pr).lower()
            out["running"] = {"phases": ["Fetching the PR", "Checking out the branch",
                                         "Building the QA guide"],
                              "cur": (0 if "fetch" in s else
                                      1 if ("checking out" in s or "queued" in s) else 2),
                              "queued": "queued" in s}
            out["stopToken"] = self._tok("qastop", pr)
        elif st == "failed":
            out["failed"] = qa_status_text(pr)
        elif st == "stopped":
            out["stopped"] = True
        elif st == "done":
            out["md"] = load_qa(pr)
        return out

    def api_skills(self, user):
        exp, sig = mint("settings", user, ACTION_TTL)
        choice, eff_lbl = effective_skill(user)
        return {
            "token": {"exp": exp, "sig": sig},
            "user": user, "choice": choice, "effLabel": eff_lbl,
            "hasMySkill": bool(read_skill(user)), "hasGlobal": bool(read_skill("global")),
            "teamSkill": read_skill("global"), "mySkill": read_skill(user),
            "depths": {lv: {"name": EFFORT[lv][0], "meta": EFFORT[lv][1],
                            "content": effort_depth(lv), "edited": effort_edited(lv)}
                       for lv in EFFORT_ORDER},
            "stats": prbot_learn.skill_stats(),
        }

    def api_integrations(self, user):
        u = load_users().get(user) or {}
        exp, sig = mint("settings", user, ACTION_TTL)
        connected = bool(u.get("claude_token_enc"))
        claude_url = ""
        if not connected:
            claude_url, _ = claude_connect_start(user)
        return {"token": {"exp": exp, "sig": sig},
                "github": {"login": user},
                "slack": {"id": u.get("slack_id", "")},
                "claude": {"connected": connected, "authUrl": claude_url or ""},
                "oauth": OAUTH_ENABLED, "brand": BRAND}

    def api_learnings(self, user):
        pk = {"dropped": "blocker", "edited": "should-fix", "kept": "posted"}
        pl = {"dropped": "dropped", "edited": "reworded", "kept": "kept"}

        def item(r):
            o = r.get("outcome")
            return {"kind": pk.get(o, "archived"), "label": pl.get(o, o or ""),
                    "loc": r.get("path", "") + (f":{r['line']}" if r.get("line") else ""),
                    "severity": r.get("severity", "nit"), "gist": r.get("gist", ""),
                    "editedGist": r.get("edited_gist", "") if o == "edited" else ""}
        return {"counts": prbot_learn.counts(), "rows": [item(r) for r in prbot_learn.recent(80)]}

    def api_stack(self, pr, user):
        stack = pr_stack(pr)
        exp, sig = mint("stackrun", pr, PAGE_TTL)
        return {"pr": pr, "isStack": len(stack) > 1, "connected": claude_connected(user),
                "runToken": {"exp": exp, "sig": sig},
                "levels": [{"key": k, "name": EFFORT[k][0], "sub": EFFORT[k][1]}
                           for k in EFFORT_ORDER],
                "stack": [{"num": str(it["number"]), "title": it.get("title", ""),
                           "base": it.get("baseRefName", ""), "head": it.get("headRefName", ""),
                           "state": pr_state(str(it["number"]), user)} for it in stack]}

    def api_me(self, user):
        if not user:
            return {"authed": False, "brand": BRAND, "repo": REPO, "dry_run": DRY_RUN,
                    "oauth": OAUTH_ENABLED, "logo": prbot_assets.LOGO}
        u = load_users().get(user) or {}
        choice, skill_label = effective_skill(user)
        return {"authed": True, "login": user, "name": u.get("name") or user,
                "slack_id": u.get("slack_id", ""), "claude_connected": claude_connected(user),
                "active_skill": choice, "skill_label": skill_label, "dry_run": DRY_RUN,
                "repo": REPO, "brand": BRAND, "oauth": OAUTH_ENABLED, "logo": prbot_assets.LOGO}

    def api_queue(self, user, tab, sort):
        if tab not in dict(TABS):
            tab = "todo"
        entries = []
        for item, active in all_prs(user):
            num = str(item.get("number"))
            st = pr_state(num, user)
            rev = load_review(num)
            cs = sev_counts(rev.get("comments", [])) if rev else {}
            t = pr_times(num, user)
            upd = iso_ts(item.get("updatedAt")) or iso_ts(item.get("createdAt"))
            entries.append({"num": num, "item": item, "active": active, "st": st, "t": t,
                            "cs": cs, "updated": upd,
                            "touched": max(t["approved"], t["posted"], t["reviewed"], upd),
                            "blockers": cs.get("blocker", 0), "total": sum(cs.values())})
        # A PR you're not currently requested on (you just opened it, or were removed as a
        # reviewer — "no longer requested") that you haven't reviewed yet does NOT belong in
        # "To review". It stays visible under "All". Reviewed/Posted/Approved still show your
        # work on PRs that have left your live queue.
        def entry_tab(e):
            tb = tab_of(e["st"])
            if tb == "todo" and not e["active"]:
                return None
            return tb
        counts = {k: 0 for k, _ in TABS}
        for e in entries:
            tb = entry_tab(e)
            if tb:
                counts[tb] += 1
            if e["st"] != "archived":
                counts["all"] += 1
        shown = [e for e in entries
                 if entry_tab(e) == tab or (tab == "all" and e["st"] != "archived")]
        keys = {"newest": lambda e: -(e["updated"] or int(e["num"])),
                "oldest": lambda e: (e["updated"] or int(e["num"])),
                "activity": lambda e: -e["touched"],
                "findings": lambda e: (-e["blockers"], -e["total"])}
        shown.sort(key=keys.get(sort, keys["newest"]))
        rows = []
        for e in shown:
            item, t, num = e["item"], e["t"], e["num"]
            when = []
            if t["approved"]:
                when.append(f"approved {fmt_date(t['approved'])}")
            elif t["posted"]:
                when.append(f"posted {fmt_date(t['posted'])}")
            elif t["reviewed"]:
                when.append(f"reviewed {ago(t['reviewed'])}")
            if e["updated"]:
                when.append(f"PR updated {fmt_date(e['updated'])}")
            if not e["active"]:
                when.append("no longer requested")
            size = (f"+{item.get('additions', 0):,} −{item.get('deletions', 0):,} · "
                    f"{item['changedFiles']} files") if item.get("changedFiles") else ""
            sev = [{"kind": k, "n": n, "label": SEV_LABEL.get(k, k)}
                   for k, n in sorted(e["cs"].items(), key=lambda kv: SEV_ORDER.get(kv[0], 9))]
            archived = e["st"] == "archived"
            aexp, asig = mint("unarchive" if archived else "archive", num, ACTION_TTL)
            rows.append({"num": num, "title": item.get("title", ""),
                         "author": item.get("author", ""), "state": e["st"], "size": size,
                         "when": when, "sev": sev, "archived": archived,
                         "archiveToken": {"exp": aexp, "sig": asig}})
        return {"tab": tab, "sort": sort,
                "tabs": [{"key": k, "label": lbl, "count": counts[k]} for k, lbl in TABS],
                "stats": {k: counts[k] for k in ("todo", "reviewed", "posted", "approved")},
                "tabDesc": TAB_DESC.get(tab, ""),
                "rows": rows,
                "slackOk": bool((load_users().get(user) or {}).get("slack_id"))}

    def api_post(self, route, body):
        if route == "/api/login":
            pat = (body.get("pat") or "").strip()
            if not pat:
                return self.api_json({"error": "Paste a token."}, 400)
            login, name, err = verify_pat(pat)
            if err:
                return self.api_json({"error": err}, 400)

            def apply(users):
                prev = users.get(login) or {}
                u = dict(prev)
                u.update({"pat_enc": enc(pat), "name": name,
                          "added": prev.get("added") or int(time.time()),
                          "updated": int(time.time())})
                users[login] = u
            modify_users(apply)
            print(f"login (api): {login}", flush=True)
            return self.api_json({"ok": True, "login": login},
                                 cookie=session_cookie(login, self.headers.get("Host", "")))
        user = session_user(self.headers)
        if not user:
            return self.api_json({"error": "unauthorized"}, 401)
        if route == "/api/logout":
            return self.api_json({"ok": True}, cookie=clear_session_cookie(self.headers.get("Host", "")))

        # Settings-token actions with no PR: skills, integrations settings, Claude connect.
        def settings_gate():
            return verify("settings", user, str(body.get("exp") or ""), str(body.get("sig") or ""))

        if route.startswith("/api/skill/"):
            step = route.rsplit("/", 1)[1]
            if err := settings_gate():
                return self.api_json({"error": err}, 403)
            form = {k: [str(v)] for k, v in body.items()}
            return self.api_json({"bannerHtml": self._skill_result(user, step, form)})
        if route == "/api/settings":
            if err := settings_gate():
                return self.api_json({"error": err}, 403)
            form = {k: [str(v)] for k, v in body.items()}
            return self.api_json({"bannerHtml": self._settings_result(user, form)})
        if route.startswith("/api/claude/"):
            step = route.rsplit("/", 1)[1]
            if err := settings_gate():
                return self.api_json({"error": err}, 403)
            form = {k: [str(v)] for k, v in body.items()}
            banner = self._claude_result(user, step, form)
            return self.api_json({"bannerHtml": banner, "connected": claude_connected(user)})

        # PR-scoped actions — all gated by the signed token in the body (same model as the forms).
        pr = str(body.get("pr") or "")
        exp, sig = str(body.get("exp") or ""), str(body.get("sig") or "")
        if not pr.isdigit():
            return self.api_json({"error": "missing pr"}, 400)

        def gate(action):
            return verify(action, pr, exp, sig)

        if route == "/api/review":
            if err := gate("review"):
                return self.api_json({"error": err}, 403)
            started = self._spawn_review(pr, user, str(body.get("effort") or ""),
                                         str(body.get("focus") or ""),
                                         str(body.get("model") or ""))
            # started is False when a previous run still holds the per-PR lock (e.g. a stop that
            # could not be confirmed). Surface it so the UI doesn't look like a silent no-op.
            return self.api_json({"ok": True, "started": started})
        if route == "/api/stop":
            if err := gate("stop"):
                return self.api_json({"error": err}, 403)
            return self.api_json({"ok": True, "confirmed": stop_review(pr, user)})
        if route == "/api/qa/gen":
            if err := gate("qa"):
                return self.api_json({"error": err}, 403)
            self._spawn_qa(pr, user)
            return self.api_json({"ok": True})
        if route == "/api/qa/stop":
            if err := gate("qastop"):
                return self.api_json({"error": err}, 403)
            return self.api_json({"ok": True, "confirmed": stop_qa(pr, user)})
        if route == "/api/stack/run":
            if err := gate("stackrun"):
                return self.api_json({"error": err}, 403)
            stack_nums = [str(it["number"]) for it in pr_stack(pr)]
            want = [n for n in (str(x) for x in (body.get("nums") or [])) if n in stack_nums]
            if not want:                              # no selection sent → review the whole stack
                want = stack_nums
            started = 0
            for n in want:
                if self._spawn_review(n, user, str(body.get("effort") or ""),
                                      model=str(body.get("model") or "")):
                    started += 1
            return self.api_json({"ok": True, "started": started})
        if route == "/api/markdone":
            if err := gate("markdone"):
                return self.api_json({"error": err}, 403)
            d = udir(pr, user)
            d.mkdir(parents=True, exist_ok=True)
            (d / "approved").write_text(json.dumps(
                {"at": int(time.time()), "manual": True,
                 "body": "Handled outside the bot — approved on GitHub directly."}))
            return self.api_json({"ok": True})
        if route == "/api/archive":
            act = "unarchive" if body.get("action") == "unarchive" else "archive"
            if err := gate(act):
                return self.api_json({"error": err}, 403)
            f = udir(pr, user) / "archived"
            f.parent.mkdir(parents=True, exist_ok=True)
            if act == "archive":
                f.write_text(str(int(time.time())))
            else:
                f.unlink(missing_ok=True)
            return self.api_json({"ok": True})
        if route == "/api/post":
            if err := gate("post"):
                return self.api_json({"error": err}, 403)
            return self.api_json({"bannerHtml": self._post_result(pr, user, self._post_form(pr, body))})
        if route == "/api/approve":
            if err := gate("approve"):
                return self.api_json({"error": err}, 403)
            form = {"pr": [pr], "ack": ["1"] if body.get("ack") else [],
                    "approve_body": [str(body.get("body") or "")]}
            return self.api_json({"bannerHtml": self._approve_result(pr, user, form)})
        return self.api_json({"error": "not found"}, 404)

    def _post_form(self, pr, body):
        """Rebuild the form dict _post_result expects from the JSON post body. path/line/severity
        come from the stored review (not the client) — only selection, body and suggestion are
        the reviewer's to change."""
        rev = load_review(pr) or {}
        originals = sorted(rev.get("comments", []),
                           key=lambda c: SEV_ORDER.get(c.get("severity"), 9))
        sel = set(body.get("selected") or [])
        bodies = body.get("bodies") or {}
        suggs = body.get("suggs") or {}
        form = {"pr": [pr], "count": [str(len(originals))]}
        if body.get("request_changes"):
            form["request_changes"] = ["on"]
        for i, c in enumerate(originals):
            if i in sel:
                form[f"sel_{i}"] = ["on"]
            form[f"body_{i}"] = [str(bodies.get(str(i), c.get("body", "")))]
            form[f"sugg_{i}"] = [str(suggs.get(str(i), c.get("suggestion", "") or ""))]
            form[f"path_{i}"] = [c.get("path", "")]
            form[f"line_{i}"] = [str(c.get("line", "") or "")]
            form[f"sev_{i}"] = [c.get("severity", "nit")]
        return form

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        route = urlparse(self.path).path.rstrip("/").removeprefix("/prbot")
        if route.startswith("/api/"):
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                body = {}
            return self.api_post(route, body if isinstance(body, dict) else {})
        return self.reply(404, "not found", "text/plain; charset=utf-8")

    # -- auth pages ----------------------------------------------------------------------------
    def oauth_callback(self, code, state, error):
        def to_login_err(msg):
            return self.redirect("/prbot/login?err=" + quote(msg))
        nxt = oauth_check_state(state)
        if nxt is None:
            return to_login_err("That sign-in link was stale or altered \u2014 try again.")
        if error or not code:
            return to_login_err("GitHub did not complete the sign-in: "
                                + (error or "no code returned"))
        d = oauth_token_request({"code": code,
                                 "redirect_uri": f"{PUBLIC_URL}/prbot/oauth/callback"})
        if not d:
            return to_login_err("GitHub rejected the sign-in code. Try again.")
        login, name, err = verify_pat(d["access_token"])
        if err:
            # Usually an org-side block, not a bad token: a GitHub App not installed on the org,
            # or an OAuth App not approved under the org's third-party access settings.
            OAUTH_BLOCKED.write_text(str(int(time.time())))
            return to_login_err(
                f"GitHub signed you in, but the token cannot see {REPO}. An org owner needs to "
                "allow this app once (OAuth App: approve under Third-party access; GitHub App: "
                "install it on the org). Until then, sign in with a token.")
        OAUTH_BLOCKED.unlink(missing_ok=True)
        prev = load_users().get(login) or {}
        oauth_store(login, d, name, prev)
        print(f"login (github): {login}", flush=True)
        if not prev.get("slack_id"):
            nxt = "/prbot/integrations?welcome=1&next=" + quote(nxt, safe="")
        return self.redirect(nxt, cookie=session_cookie(login, self.headers.get("Host", "")))

    def _claude_result(self, user, step, form):
        """Claude connect steps (cancel/disconnect/code) → banner HTML. Settings token assumed
        verified. `start` is handled in the HTML wrapper / api_integrations (it mints the URL)."""
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        if step == "cancel":
            claude_connect_cancel(user)
            return ""
        if step == "disconnect":
            def apply(users):
                uu = users.get(user) or {}
                for k in ("claude_token_enc", "claude_refresh_enc", "claude_exp", "claude_added"):
                    uu.pop(k, None)
                users[user] = uu
            modify_users(apply)
            claude_connect_cancel(user)
            return ("<div class='banner ok'><span>✓</span><div>Claude disconnected — you'll need "
                    "to reconnect to run reviews.</div></div>")
        code = one("code").strip()
        if not code:
            return ("<div class='banner warn'><span>⚠️</span><div>Paste the code Claude showed "
                    "you.</div></div>")
        result, err = claude_connect_code(user, code)
        if err:
            return f"<div class='banner err'><span>🚫</span><div>{html.escape(err)}</div></div>"
        ok, why = verify_claude_token(result["access_token"])
        if not ok:
            return (f"<div class='banner err'><span>🚫</span><div>Got a token from Claude but it "
                    f"did not work here: <code>{html.escape(why)}</code></div></div>")
        store_claude_token(user, result)
        print(f"claude connected: {user}", flush=True)
        return ("<div class='banner ok'><span>✓</span><div>Claude connected — reviews you start "
                "now run on your own account.</div></div>")

    def _skill_result(self, user, step, form):
        """Apply a skill/effort-depth edit and return a banner HTML string (reused by the HTML
        page and the JSON API). Assumes the settings token is already verified."""
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        # Which skill this edits: the team default (shared) or the user's own.
        target = "global" if one("target") == "global" else user
        who = ("the team default skill" if target == "global" else "your skill")
        err_b = lambda m: f"<div class='banner err'><span>🚫</span><div>{m}</div></div>"  # noqa
        ok = lambda m: f"<div class='banner ok'><span>✓</span><div>{m}</div></div>"  # noqa

        # Review-depth instructions (Quick/Standard/Deep) — shared, editable, with reset-to-default.
        tgt = one("target")
        if tgt.startswith("effort_"):
            level = tgt[len("effort_"):]
            if level not in EFFORT:
                return err_b("Unknown depth level.")
            name = EFFORT[level][0]
            if step == "reset":
                effort_depth_path(level).unlink(missing_ok=True)
                return ok(f"Reset the <b>{name}</b> depth to the built-in default.")
            text = one("skill")
            if not text.strip():
                return err_b("The depth instruction can't be empty. Use Reset to restore the "
                             "default.")
            if len(text) > 20000:
                return err_b("That's very large (>20k chars). Trim it.")
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            effort_depth_path(level).write_text(text)
            print(f"effort depth saved: {level} ({len(text)} chars)", flush=True)
            return ok(f"Saved the <b>{name}</b> review depth — it applies to every {name} review.")

        # Which skill runs my reviews — my own, or the shared team default.
        if step == "use":
            set_active_skill(user, one("choice"))
            _, lbl = effective_skill(user)
            return ok(f"Your reviews now run with <b>{lbl}</b>.")

        if step == "rule":
            rule = one("rule")
            if not rule.strip():
                return err_b("Type a rule to add.")
            new_text = add_skill_rule(read_skill(target), rule)
            if len(new_text) > 40000:
                return err_b("That skill is already very large (>40k chars). Trim it first.")
            save_skill(target, new_text)
            print(f"skill rule added to {target}: {tidy_rule(rule)!r}", flush=True)
            return ok(f"Added to {who} — Robin will apply it on every review: "
                      f"<b>{html.escape(tidy_rule(rule))}</b>")

        # The team default is shared — restoring the built-in wipes everyone's edits, so it takes
        # a typed confirmation and lives on its own step. A plain "reset" only clears a personal skill.
        if step == "restore":
            if target != "global":
                return err_b("Nothing to restore.")
            if one("confirm").strip().upper() != "RESTORE":
                return err_b("Type RESTORE to confirm — this discards the team's edits for "
                             "everyone.")
            restore_global_skill()
            print("team default skill restored to installed default", flush=True)
            return ok("Team default restored to the built-in review skill.")

        if step == "reset":
            if target == "global":
                return err_b("The team default can't be reset here — use “Restore built-in” "
                             "with confirmation.")
            save_skill(user, "")
            return ok("Cleared your skill — your reviews use the team default now.")

        text = one("skill")
        if target == "global" and not text.strip():
            return err_b("The team default can't be emptied — everyone relies on it. To go back "
                         "to the built-in skill, use “Restore built-in”.")
        if len(text) > 40000:
            return err_b("That skill is very large (>40k chars). Trim it and try again.")
        save_skill(target, text)
        print(f"skill saved: {target} ({len(text)} chars)", flush=True)
        return ok(f"Saved {who} — reviews now use it (with Robin's output format appended).")

    def _settings_result(self, user, form):
        """Save the Slack ID and/or replace the GitHub PAT → banner HTML. Settings token assumed
        verified. Only touches a field the form actually sent."""
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        err_b = lambda m: f"<div class='banner err'><span>🚫</span><div>{m}</div></div>"  # noqa
        slack_val = one("slack_id").strip() if "slack_id" in form else None
        pat = one("pat").strip()
        new_pat_enc = new_name = None
        if pat:
            login, name, err = verify_pat(pat)
            if err:
                return err_b(html.escape(err))
            if login != user:
                return err_b(f"That token belongs to <code>{html.escape(login)}</code>, not you.")
            new_pat_enc, new_name = enc(pat), name

        def apply(users):
            u = users.get(user) or {}
            if slack_val is not None:
                u["slack_id"] = slack_val
            if new_pat_enc is not None:
                u["pat_enc"], u["name"] = new_pat_enc, new_name
            u["updated"] = int(time.time())
            users[user] = u
        modify_users(apply)
        return "<div class='banner ok'><span>✓</span><div>Saved.</div></div>"

    def _spawn_review(self, pr, user, effort="", focus="", model=""):
        """Queue one review (no redirect). Returns True if it actually spawned, False if a review
        was already running for that PR. Shared by start_review and the stack runner."""
        pr = str(pr)
        if not claude_connected(user):
            return False                            # reviews require the user's own Claude account
        d = STATE / pr
        d.mkdir(parents=True, exist_ok=True)
        touch_user(pr, user)
        # run-review.sh takes a per-PR flock, so a genuine duplicate is impossible — only skip
        # when a review is ACTUALLY running. This lets a finished review be re-run and, crucially,
        # a stalled one (status stuck at "reviewing" but the process is gone) be recovered.
        if is_running(pr):
            return False
        meta, _ = pr_meta(pr)
        eff = effort if effort in EFFORT else autosize_effort(meta)
        focus = (focus or "").strip()[:2000]
        archive_review(pr)                          # keep the prior run in history/
        mdl = model if model in MODEL_KEYS else ""
        (d / "effort").write_text(eff)
        (d / "focus").write_text(focus)
        (d / "model").write_text(mdl)
        (d / "status").write_text("queued")
        choice, _ = effective_skill(user)
        env = review_env(user)
        env["PRBOT_EFFORT"] = eff
        env["PRBOT_DEPTH"] = effort_depth(eff)
        env["PRBOT_FOCUS"] = focus
        env["PRBOT_MODEL"] = mdl
        env["PRBOT_SKILL_CHOICE"] = choice
        # Stack context: if this PR is stacked on other open PRs, its diff is only its own changes.
        # Tell the agent the siblings exist so it doesn't flag setup a lower PR provides.
        try:
            stack = pr_stack(pr)
        except Exception:
            stack = []
        if len(stack) > 1:
            rows = "\n".join(
                f"  #{it.get('number')} \u2014 {it.get('title', '')}"
                + ("  \u2190 this PR" if str(it.get("number")) == pr else "")
                for it in stack)
            env["PRBOT_STACK"] = (
                f"This PR is part of a stack of {len(stack)} open PRs (each based on the one "
                "above). The diff you see is ONLY this PR's own changes \u2014 assume the changes "
                "from the other PRs in the stack are already present. Do not flag missing "
                "definitions, imports, migrations or setup that another PR in the stack provides; "
                "do consider cross-PR dependencies and whether this PR is coherent on top of the "
                "ones below it.\nStack (top \u2192 bottom):\n" + rows)
        with open(d / "run.log", "ab") as log:
            proc = subprocess.Popen([str(BIN / "run-review.sh"), pr], stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True, env=env)
        (d / "pid").write_text(str(proc.pid))
        return True

    # --- QA guides ---------------------------------------------------------------------------
    def _spawn_qa(self, pr, user):
        if not claude_connected(user):              # QA runs Claude too — needs their own account
            return False
        d = STATE / pr
        d.mkdir(parents=True, exist_ok=True)
        if qa_running(pr):
            return False
        (d / "qa.status").write_text("queued")
        env = review_env(user)                  # runs on the clicker's Claude account
        with open(d / "qa.log", "ab") as log:
            proc = subprocess.Popen([str(BIN / "run-qa.sh"), pr], stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True, env=env)
        (d / "qa.pid").write_text(str(proc.pid))
        return True

    def _post_result(self, pr, user, form):
        """Post the selected comments; returns a banner HTML string (reused by the HTML page and
        the JSON API)."""
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        # Don't post a review that is still being (re)generated — the review.json on disk may be
        # the previous run's, and posting it produces a half-built comment on the real PR.
        if is_running(pr):
            return ("<div class='banner warn'><span>⏳</span><div>A review is still running "
                    "for this PR — wait for it to finish, then post.</div></div>")
        rev = load_review(pr) or {}
        chosen = []
        blank = []
        for i in range(int(one("count") or 0)):
            if not form.get(f"sel_{i}"):
                continue
            line = one(f"line_{i}")
            body = one(f"body_{i}").strip()
            # A suggested change becomes a GitHub ```suggestion block appended to the comment,
            # which GitHub renders with a one-click "Apply" for the author on the anchored line.
            sugg = one(f"sugg_{i}").rstrip("\n")
            if sugg.strip():
                body = f"{body}\n\n```suggestion\n{sugg}\n```"
            # A selected finding whose text was cleared would be silently dropped downstream
            # (empty-body comments are skipped), so it never reaches GitHub and never folds into
            # the summary — the reviewer thinks they posted it. Catch it and refuse instead.
            if not body.strip():
                loc = one(f"path_{i}") + (f":{line}" if line.isdigit() else "")
                blank.append(loc or f"finding {i + 1}")
            chosen.append({"path": one(f"path_{i}"),
                           "line": int(line) if line.isdigit() else None,
                           "severity": one(f"sev_{i}"),
                           "body": body})
        if blank:
            items = ", ".join(f"<code>{html.escape(b)}</code>" for b in blank)
            return ("<div class='banner warn'><span>⚠️</span><div>These selected "
                    f"finding(s) have no text: {items}. Add a comment or unselect them before "
                    "posting.</div></div>")
        # Learnings: capture what was dropped/edited/kept before posting — the same signal the
        # dashboard used to discard. Sorted like review_body so form index i lines up. Done
        # even when nothing is chosen (dropping every finding is the strongest signal), and
        # before the dry-run branch so it learns during the pilot too.
        originals = sorted(rev.get("comments", []),
                           key=lambda c: SEV_ORDER.get(c.get("severity"), 9))
        skill_f = STATE / pr / "skill"
        skill = skill_f.read_text().strip() if skill_f.exists() else "global"
        prbot_learn.record(pr, user, originals, form, skill=skill)
        if not chosen:
            return ("<div class='banner warn'><span>⚠️</span><div>"
                    "Nothing selected — nothing sent.</div></div>")

        # Re-validate anchors against the CURRENT diff: the PR may have gained commits while
        # this review sat in the dashboard, and one stale line 422s the whole review.
        files, err = fetch_pr_files(pr)
        if err is not None:
            return (
                f"<div class='banner err'><span>🔴</span><div><b>Could not fetch the PR diff "
                f"from GitHub — nothing was posted.</b><br>Without it every comment would be "
                f"demoted out of the diff and posted as a plain summary, so this refuses "
                f"rather than posting a degraded review. Check "
                f"<a href='https://www.githubstatus.com' target=_blank rel=noopener>"
                f"githubstatus.com</a> and retry.<br>"
                f"<code>{html.escape(err)}</code></div></div>")
        inline, orphans = prbot_diff.split_anchorable(chosen, prbot_diff.anchor_map(files))
        # No bot signature: this posts under the reviewer's own account, so GitHub already
        # attributes it. A trailing "Reviewed by @x" only restates the byline.
        body = (rev.get("summary") or "").strip() + prbot_diff.orphan_block(orphans)
        # A COMMENT review with an empty body and no inline comments is a half-built post — refuse
        # it. (Can happen if the review has no summary and every selected finding failed to anchor.)
        if not body.strip() and not inline:
            return ("<div class='banner warn'><span>⚠️</span><div>Nothing to post — the "
                    "review has no summary and none of the selected findings could be anchored to "
                    "the current diff.</div></div>")
        # Default is a plain COMMENT review. The reviewer can deliberately choose REQUEST_CHANGES
        # from the post bar (never the agent's call) — a human-only, blocking action.
        event = "REQUEST_CHANGES" if form.get("request_changes") else "COMMENT"
        payload = {"body": body, "event": event, "comments": inline}
        ud = udir(pr, user)
        ud.mkdir(parents=True, exist_ok=True)
        (ud / "payload.json").write_text(json.dumps(payload))

        if DRY_RUN:
            return (
                f"<div class='banner warn'><span>🧪</span><div><b>DRY RUN — nothing was sent "
                f"to GitHub.</b><br>Would post {len(inline)} inline comment(s)"
                + (f", {len(orphans)} folded into the summary" if orphans else "")
                + f", as <code>{event}</code>. Set <code>DRY_RUN=0</code> and restart "
                  f"<code>prbot</code> to post for real.</div></div>")

        tok = user_pat(user)
        if not tok:
            return ("<div class='banner err'><span>🚫</span><div>"
                    "Your stored GitHub token could not be read — "
                    "paste it again in <a href='/prbot/integrations'>settings</a>.</div></div>")
        r = gh(["api", "--method", "POST", f"repos/{REPO}/pulls/{pr}/reviews",
                "--input", str(ud / "payload.json")], token=tok)
        if r.returncode != 0:
            return (f"<div class='banner err'><span>🔴</span><div>GitHub rejected it: <code>"
                    f"{html.escape(r.stderr[:400])}</code></div></div>")
        (ud / "posted.json").write_text(
            json.dumps({"at": int(time.time()), "inline": len(inline), "event": event}))
        return (
            f"<div class='banner ok'><span>✓</span><div>Posted {len(inline)} comment(s) as "
            f"<code>{event}</code> under <code>{html.escape(user)}</code>."
            + (f" {len(orphans)} could not be anchored and went into the summary."
               if orphans else "") + "</div></div>")

    def _approve_result(self, pr, user, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        rev = load_review(pr) or {}
        blockers = sev_counts(rev.get("comments", [])).get("blocker", 0)
        lgtm = blockers == 0 and rev.get("event") != "REQUEST_CHANGES"
        if not lgtm and not one("ack"):
            return ("<div class='banner warn'><span>⚠️</span><div>This review is not LGTM — tick "
                    "the confirmation to approve anyway.</div></div>")
        msg = one("approve_body").strip() or "LGTM."
        tok = user_pat(user)
        if not tok:
            return ("<div class='banner err'><span>🚫</span><div>Your stored GitHub token could "
                    "not be read — paste it again in <a href='/prbot/integrations'>settings</a>."
                    "</div></div>")
        ok, why = can_approve(pr, user)
        if not ok:
            return f"<div class='banner err'><span>🚫</span><div>{html.escape(why)}</div></div>"
        if DRY_RUN:
            return (
                f"<div class='banner warn'><span>🧪</span><div><b>DRY RUN — not approved.</b>"
                f"<br>Would submit an APPROVE review as <code>{html.escape(user)}</code> with "
                f"body: <em>{html.escape(msg[:200])}</em></div></div>")
        r = gh(["api", "--method", "POST", f"repos/{REPO}/pulls/{pr}/reviews",
                "-f", "event=APPROVE", "-f", f"body={msg}"], token=tok)
        if r.returncode != 0:
            return (f"<div class='banner err'><span>🔴</span><div>GitHub rejected it: <code>"
                    f"{html.escape(r.stderr[:400])}</code></div></div>")
        ud = udir(pr, user)
        ud.mkdir(parents=True, exist_ok=True)
        (ud / "approved").write_text(json.dumps({"at": int(time.time()), "body": msg}))
        return (f"<div class='banner ok'><span>✅</span><div>Approved #{pr} as "
                f"<code>{html.escape(user)}</code>.</div></div>")


if __name__ == "__main__":
    port = int(os.environ.get("PRBOT_PORT", "8899"))
    if USERS.exists():
        os.chmod(USERS, 0o600)
    print(f"prbot listening on 127.0.0.1:{port} (dry_run={DRY_RUN}, "
          f"users={len(load_users())})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
