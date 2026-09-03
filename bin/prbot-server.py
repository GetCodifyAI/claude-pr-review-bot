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
    f"https://prbot-{ENV.get('PRBOT_ENV', '')}.{ENV.get('PRBOT_DOMAIN', 'staging.eng.cutanddry.com')}")
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
    users = load_users()
    users[login] = u
    save_users(users)


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
    users = load_users()
    u = users.get(login) or {}
    u["claude_token_enc"] = enc(data["access_token"])
    u["claude_exp"] = now + int(data["expires_in"]) if data.get("expires_in") else 0
    u["claude_added"] = u.get("claude_added") or now
    u["updated"] = now
    if data.get("refresh_token"):
        u["claude_refresh_enc"] = enc(data["refresh_token"])
    users[login] = u
    save_users(users)


def review_env(login):
    """Environment for a review spawned by `login`: which account it bills to, and whose review
    skill it uses. PRBOT_ACTOR is always the clicker (drives skill choice); PRBOT_RUN_AS is the
    Claude account for display, only their own if they connected one."""
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
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    p = skill_path(login)
    if text.strip():
        p.write_text(text)
    elif login != "global":
        p.unlink(missing_ok=True)          # empty personal skill => fall back to the team default
    else:
        p.unlink(missing_ok=True)          # empty team default => fall back to the installed skill


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


def session_cookie(login):
    exp = int(time.time()) + SESSION_TTL
    return (f"prbot_s={login}:{exp}:{session_sig(login, exp)}; Path=/prbot; "
            f"Max-Age={SESSION_TTL}; HttpOnly; Secure; SameSite=Lax")


def clear_session_cookie():
    return "prbot_s=; Path=/prbot; Max-Age=0; HttpOnly; Secure; SameSite=Lax"


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


# --- css / shell --------------------------------------------------------------------------
CSS = """
*{box-sizing:border-box}
:root{
--bg:#0a0b12;--panel:#12141d;--panel2:#171a25;--line:#242838;--line2:#1b1e2a;
--hair:rgba(255,255,255,.06);--fg:#e9ebf3;--dim:#9aa0b4;--faint:#6b7186;
--blue:#5b7cfa;--purple:#a855f7;--pink:#ec4899;
--grad:linear-gradient(135deg,#5b7cfa 0%,#a855f7 52%,#ec4899 100%);
--ok:#3ddc97;--okbg:rgba(61,220,151,.10);--okln:rgba(61,220,151,.30);
--warn:#f5b64a;--warnbg:rgba(245,182,74,.10);--warnln:rgba(245,182,74,.30);
--err:#ff7089;--errbg:rgba(255,112,137,.10);--errln:rgba(255,112,137,.32);
--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 28px -12px rgba(0,0,0,.6);
--r:14px;--font:'Inter',-apple-system,system-ui,"Segoe UI",sans-serif;
--mono:ui-monospace,'SF Mono',SFMono-Regular,Menlo,monospace}
html{-webkit-text-size-adjust:100%}
body{font:15px/1.65 var(--font);background:var(--bg);color:var(--fg);margin:0;
padding:0;letter-spacing:-.006em;
background-image:radial-gradient(900px 380px at 50% -160px,rgba(91,124,250,.10),transparent 70%)}
::selection{background:rgba(168,85,247,.32)}
a{color:var(--blue);text-decoration:none}a:hover{color:#8aa2ff}
h1{font-size:1.55rem;font-weight:700;margin:0 0 6px;line-height:1.25;letter-spacing:-.02em}
h2{font-size:.74rem;margin:32px 0 12px;color:var(--faint);text-transform:uppercase;
letter-spacing:.09em;font-weight:700}
h4,h5,h6{font-size:.98rem;font-weight:650;margin:20px 0 8px;color:var(--fg);
letter-spacing:-.01em}
p{margin:10px 0}
.muted{color:var(--dim)}.sm{font-size:.87rem}
.grad-text{background:var(--grad);-webkit-background-clip:text;background-clip:text;
-webkit-text-fill-color:transparent;color:transparent}
/* top bar */
.topbar{position:sticky;top:0;z-index:30;display:flex;align-items:center;
height:56px;padding:0 20px;background:rgba(10,11,18,.72);backdrop-filter:saturate(140%) blur(14px);
-webkit-backdrop-filter:saturate(140%) blur(14px);border-bottom:1px solid var(--hair)}
.topbar::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;background:var(--grad)}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:1.02rem;
color:var(--fg);letter-spacing:-.02em}
.brand:hover{color:var(--fg)}
.brand img{width:28px;height:28px;display:block;filter:drop-shadow(0 2px 6px rgba(168,85,247,.35))}
.brand .n{background:var(--grad);-webkit-background-clip:text;background-clip:text;
-webkit-text-fill-color:transparent}
.brand .tag{color:var(--faint);font-weight:500;font-size:.82rem;margin-left:2px;
border-left:1px solid var(--line);padding-left:10px}
@media(max-width:520px){.brand .tag{display:none}}
.navr{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:.85rem}
.navr a{color:var(--dim)}.navr a:hover{color:var(--fg)}
.navr .who{display:inline-flex;align-items:center;gap:7px;color:var(--fg);font-weight:550;
background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:4px 12px 4px 5px}
.navr .who .av{width:22px;height:22px;border-radius:50%;background:var(--grad);color:#fff;
display:inline-flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:700}
.navr .live{font-size:.7rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;
padding:3px 9px;border-radius:999px}
.navr .live.on{background:var(--okbg);color:#6fe6b2;border:1px solid var(--okln)}
.navr .live.dry{background:var(--warnbg);color:#f6cd8a;border:1px solid var(--warnln)}
@media(max-width:560px){.navr .who span.nm,.navr .lbl{display:none}}
/* stat strip */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0 4px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;
box-shadow:var(--shadow);transition:transform .15s,border-color .15s;display:block}
.stat:hover{transform:translateY(-2px);border-color:#31384d;text-decoration:none}
.stat .k{font-size:2rem;font-weight:750;letter-spacing:-.03em;line-height:1;color:var(--fg)}
.stat.hot .k{background:var(--grad);-webkit-background-clip:text;background-clip:text;
-webkit-text-fill-color:transparent}
.stat .l{color:var(--dim);font-size:.86rem;margin-top:6px;display:flex;align-items:center;gap:6px}
.stat.on{border-color:var(--purple);box-shadow:0 0 0 1px rgba(168,85,247,.3),var(--shadow)}
.tabdesc{color:var(--dim);font-size:.88rem;margin:12px 2px 14px;min-height:1.2em}
.empty{padding:44px 20px;text-align:center;color:var(--dim)}
.empty .ic{font-size:2rem;display:block;margin-bottom:8px;opacity:.7}
.empty b{color:var(--fg);display:block;font-weight:600;margin-bottom:3px}
.wrap{max-width:64rem;margin:0 auto;padding:30px 20px 0;animation:rise .5s cubic-bezier(.2,.7,.2,1) both}
.crumb{font-size:.87rem;margin:0 0 14px}
.bc{display:flex;align-items:center;gap:9px;font-size:.85rem;margin:0 0 14px;font-weight:500}
.bc a{color:var(--dim)}.bc a:hover{color:var(--fg)}
.bc .sep{color:var(--line)}.bc .cur{color:var(--fg);font-family:var(--mono);font-size:.82rem}
.prtitle{margin:0 0 12px;line-height:1.22}
.meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:.87rem;
color:var(--dim);margin:0 0 6px}
code{background:var(--panel2);border:1px solid var(--line2);padding:1px 6px;border-radius:6px;
font-size:.85em;font-family:var(--mono)}
pre{background:#0b0d15;border:1px solid var(--line2);border-radius:11px;padding:14px 16px;
overflow-x:auto;margin:12px 0}pre code{border:0;padding:0;background:none;font-size:.83rem}
blockquote{border-left:2px solid var(--purple);margin:12px 0;padding:2px 0 2px 14px;color:var(--dim)}
ul,ol{margin:10px 0;padding-left:22px}li{margin:5px 0}
.tw{overflow-x:auto;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:.89rem}
th,td{text-align:left;padding:9px 11px;border-bottom:1px solid var(--line2);vertical-align:top}
th{color:var(--faint);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em}
tbody tr:last-child td{border-bottom:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
padding:20px 22px;box-shadow:var(--shadow);animation:rise .5s cubic-bezier(.2,.7,.2,1) both}
/* pills */
.pill{display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;
font-size:.7rem;font-weight:700;letter-spacing:.03em;text-transform:uppercase;
white-space:nowrap;border:1px solid transparent}
.blocker{background:var(--errbg);color:#ff8ca0;border-color:var(--errln)}
.should-fix{background:var(--warnbg);color:#f7c26b;border-color:var(--warnln)}
.nit{background:rgba(91,124,250,.12);color:#9db4ff;border-color:rgba(91,124,250,.32)}
.question{background:rgba(168,85,247,.14);color:#cba6f7;border-color:rgba(168,85,247,.34)}
.new{background:rgba(91,124,250,.12);color:#9db4ff;border-color:rgba(91,124,250,.30)}
.reviewing{background:var(--warnbg);color:#f7c26b;border-color:var(--warnln)}
.done{background:rgba(91,124,250,.12);color:#9db4ff;border-color:rgba(91,124,250,.30)}
.posted{background:var(--okbg);color:#6fe6b2;border-color:var(--okln)}
.approved{background:var(--okbg);color:#6fe6b2;border-color:var(--okln)}
.failed{background:var(--errbg);color:#ff8ca0;border-color:var(--errln)}
.dry{background:var(--warnbg);color:#f7c26b;border-color:var(--warnln)}
.stalled{background:var(--errbg);color:#ff8ca0;border-color:var(--errln)}
.archived{background:var(--panel2);color:var(--dim);border-color:var(--line)}
/* collapsibles */
details{border:1px solid var(--line);border-radius:var(--r);background:var(--panel);margin:12px 0}
details[open]{padding-bottom:6px}
summary{cursor:pointer;padding:15px 20px;font-weight:600;font-size:.94rem;list-style:none;
display:flex;justify-content:space-between;align-items:center;gap:10px}
summary::-webkit-details-marker{display:none}
summary::after{content:"▾";color:var(--faint);font-size:.85rem;transition:transform .2s}
details[open]>summary::after{transform:rotate(180deg)}
summary:hover{color:#fff}
.dbody{padding:2px 20px 6px;border-top:1px solid var(--line2);margin-top:2px}
/* findings */
.finding{border:1px solid var(--line);border-radius:var(--r);margin:12px 0;background:var(--panel);
overflow:hidden;box-shadow:var(--shadow);transition:opacity .2s,border-color .2s;
animation:rise .5s cubic-bezier(.2,.7,.2,1) both}
.finding.off{opacity:.45}
.fhead{display:flex;gap:11px;align-items:center;flex-wrap:wrap;padding:14px 18px;
background:linear-gradient(180deg,var(--panel2),var(--panel));border-bottom:1px solid var(--line2)}
.loc{font-family:var(--mono);font-size:.82rem;color:#c3c9de;word-break:break-all}
.thread{font-size:.75rem;color:var(--faint);margin-left:auto;white-space:nowrap}
.fbody{padding:15px 18px}
.sugg{margin-top:10px;border:1px solid rgba(61,220,151,.28);border-radius:10px;overflow:hidden;
background:rgba(61,220,151,.05)}
.sugglabel{padding:8px 12px;font-size:.8rem;color:#8fecc2;border-bottom:1px solid rgba(61,220,151,.2)}
.suggin{border:0;border-radius:0;background:#0a1a13;min-height:52px;color:#c7f2dd}
.suggin:focus{box-shadow:none;outline:none}
.maybe{margin:14px 0 0;border-style:dashed;border-color:var(--line)}
.maybe>summary{color:var(--dim);font-weight:600}
.maybe .finding{margin:10px 0}
/* review progress panel */
.prog-hd{font-size:1.05rem;font-weight:600;margin-bottom:14px}
.prog{list-style:none;padding:0;margin:0}
.prog li{display:flex;align-items:center;gap:12px;padding:8px 0;color:var(--faint);font-size:.95rem}
.prog li.done{color:var(--dim)}.prog li.now{color:var(--fg);font-weight:550}
.prog .pm{width:20px;height:20px;flex:none;display:inline-flex;align-items:center;
justify-content:center;font-size:.85rem}
.prog li.done .pm{color:#6fe6b2}
.prog .pm.spin{width:15px;height:15px;border:2px solid rgba(139,164,255,.3);border-top-color:var(--blue);
border-radius:50%;animation:spin .7s linear infinite}
.progbar{height:6px;border-radius:999px;background:var(--panel2);overflow:hidden;margin:16px 0 2px;
position:relative}
.progfill{position:absolute;left:0;top:0;height:100%;width:36%;border-radius:999px;
background:var(--grad);animation:slide 1.5s ease-in-out infinite}
@keyframes slide{0%{left:-36%}100%{left:100%}}
/* inputs */
textarea{width:100%;background:#0b0d15;color:var(--fg);border:1px solid var(--line);
border-radius:11px;padding:12px 14px;font:13px/1.6 var(--mono);resize:vertical;min-height:150px;
transition:border-color .18s,box-shadow .18s}
textarea:focus,input[type=text]:focus,input[type=password]:focus{outline:0;
border-color:var(--purple);box-shadow:0 0 0 3px rgba(168,85,247,.18)}
textarea.short{min-height:76px}
input[type=checkbox]{width:18px;height:18px;accent-color:var(--purple);cursor:pointer;flex:none}
label{cursor:pointer}
select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:9px;
padding:8px 11px;font-size:.88rem}
input[type=text],input[type=password]{width:100%;background:#0b0d15;color:var(--fg);
border:1px solid var(--line);border-radius:11px;padding:11px 13px;font:14px/1.4 var(--mono);
transition:border-color .18s,box-shadow .18s}
.field{display:flex;gap:8px;align-items:stretch}.field input{flex:1;min-width:0}
/* buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;background:var(--panel2);
color:var(--fg);border:1px solid transparent;padding:10px 17px;border-radius:11px;font-size:.9rem;
cursor:pointer;font-weight:600;font-family:inherit;letter-spacing:-.01em;
transition:transform .15s cubic-bezier(.2,.7,.2,1),background .15s,border-color .15s,box-shadow .15s}
.btn:hover{background:#2a3145;transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn.primary{background:#5b56e0;border-color:transparent;color:#fff}
.btn.primary:hover{background:#6a65ec;transform:translateY(-1px)}
.btn.warn{background:#e5495f;border-color:transparent;color:#fff}
.btn.warn:hover{background:#ef5468}
.btn.ghost{background:transparent;border-color:transparent;color:var(--dim)}
.btn.ghost:hover{background:var(--panel2);color:var(--fg)}
.btn.sm{padding:7px 13px;font-size:.82rem;border-radius:9px}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}
.btn.soft{background:#242b3e}.btn.soft:hover{background:#2a3145}
/* no focus outline on clickable chrome — the hover/active states already read; inputs keep
   their focus ring (defined on .in / textarea) */
a:focus,button:focus,summary:focus,.btn:focus,.tab:focus,.stat:focus,.sortopt:focus,
.ni:focus,.row a:focus,.rowact:focus,.peek:focus{outline:none}
:focus-visible{outline:2px solid rgba(168,85,247,.6);outline-offset:2px}
.spin{width:13px;height:13px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;
border-radius:50%;display:inline-block;animation:spin .6s linear infinite}
/* post-selected action bar — inline at the end of the findings form (not fixed, so it never
   overlaps the Approve/Housekeeping sections below it) */
.bar{background:var(--panel2);border:1px solid var(--line);border-radius:14px;margin:14px 0 0;
box-shadow:var(--shadow)}
.bar .inner{padding:14px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.spacer{flex:1}
/* banners */
.banner{border-radius:12px;padding:14px 16px;margin:14px 0;font-size:.9rem;display:flex;gap:11px;
align-items:flex-start;border:1px solid;position:relative;overflow:hidden}
.banner::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px}
.banner.warn{background:var(--warnbg);border-color:var(--warnln);color:#f6cd8a}
.banner.warn::before{background:var(--warn)}
.banner.ok{background:var(--okbg);border-color:var(--okln);color:#8fecc2}
.banner.ok::before{background:var(--ok)}
.banner.err{background:var(--errbg);border-color:var(--errln);color:#ffa0b2}
.banner.err::before{background:var(--err)}
.banner.info{background:rgba(91,124,250,.10);border-color:rgba(91,124,250,.32);color:#bcd0ff}
.banner.info::before{background:#5b7cfa}
.banner b{color:#fff}
/* review effort picker */
.effort{margin-top:4px}
.effort-lbl{font-size:.82rem;color:var(--dim);margin-bottom:8px}
.effrow{display:flex;gap:9px;flex-wrap:wrap}
.btn.eff{flex:1 1 150px;flex-direction:column;align-items:flex-start;gap:2px;padding:10px 13px;
height:auto;text-align:left}
.effname{font-weight:650;font-size:.9rem}
.effsub{font-size:.72rem;opacity:.8;font-weight:400}
.btn.eff.soft .effsub{color:var(--dim)}
.rerun{white-space:nowrap}.rerun a{color:var(--dim)}.rerun a:hover{color:var(--fg)}
.effbadge{font-size:.68rem;font-weight:700;letter-spacing:.03em;text-transform:uppercase;
padding:3px 9px;border-radius:999px;background:rgba(91,124,250,.16);color:#bcd0ff;
border:1px solid rgba(91,124,250,.3)}
/* quick-add rule */
.rulebox{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.rule-lbl{font-size:.82rem;font-weight:650;color:var(--fg);margin-bottom:8px}
.rulerow{display:flex;gap:9px;align-items:stretch}.rulerow .in{flex:1;min-width:0}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0 0}
/* tabs */
.tabs{display:flex;gap:2px;flex-wrap:wrap;margin:22px 0 0;border-bottom:1px solid var(--line)}
.tab{position:relative;padding:11px 15px;font-size:.88rem;color:var(--dim);display:flex;gap:7px;
align-items:center;border-radius:9px 9px 0 0;transition:color .15s,background .15s}
.tab:hover{color:var(--fg);background:var(--panel)}
.tab.on{color:var(--fg);font-weight:650}
.tab.on::after{content:"";position:absolute;left:8px;right:8px;bottom:-1px;height:2px;
background:var(--grad);border-radius:2px}
.cnt{background:var(--panel2);border-radius:999px;padding:1px 8px;font-size:.72rem;color:var(--dim)}
.tab.on .cnt{background:rgba(168,85,247,.18);color:#cba6f7}
.sortbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:16px 0 12px}
.sortopt{font-size:.82rem;color:var(--dim);padding:5px 11px;border-radius:999px;
border:1px solid transparent;transition:background .15s,color .15s}
.sortopt:hover{color:var(--fg);background:var(--panel)}
.sortopt.on{color:var(--fg);background:var(--panel2);border-color:var(--line)}
.rowsub{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.rowsub>span:not(.chipwrap):not(:last-child)::after{content:"·";margin-left:6px;color:var(--faint)}
.chipwrap{display:inline-flex;gap:5px;flex-wrap:wrap}
/* timeline */
.timeline{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0 0;
font-size:.83rem;color:var(--dim)}
.step{display:flex;gap:6px;align-items:center;background:var(--panel);border:1px solid var(--line2);
border-radius:999px;padding:5px 13px}
.step.hit{border-color:var(--okln);color:#8fecc2;background:var(--okbg)}
/* queue list */
.list{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;
box-shadow:var(--shadow)}
.row{display:flex;align-items:center;border-bottom:1px solid var(--line2);transition:background .13s}
.row:last-child{border-bottom:0}
.row:hover{background:var(--panel2)}
.rowmeta{display:flex;align-items:center;gap:12px;flex:none;padding-right:14px}
.rowmeta .pill{min-width:78px;justify-content:center}
.rowact{font-size:.78rem;color:var(--dim);padding:6px 13px;border:1px solid var(--line2);
border-radius:9px;white-space:nowrap;transition:background .13s,color .13s}
.rowact:hover{color:var(--fg);background:var(--panel2)}
.rowlink{display:block;flex:1;min-width:0;padding:15px 4px 15px 18px}
.rowlink:hover{text-decoration:none}
.chev{color:var(--faint);font-size:1.2rem;flex:none;line-height:1}
.row:hover .chev{color:var(--blue)}
.rowtop{display:flex;gap:10px;align-items:center;margin-bottom:4px}
.num{font-family:var(--mono);font-weight:650;background:var(--grad);-webkit-background-clip:text;
background-clip:text;-webkit-text-fill-color:transparent}
.ttl{color:var(--fg);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
font-weight:550}
.lead{font-size:1.05rem;line-height:1.55;color:var(--dim);margin:6px 0 22px;max-width:40rem}
.fine{font-size:.8rem;color:var(--faint);margin:14px 0 0}
.hint{font-size:.85rem;color:var(--dim);margin:8px 0 0;min-height:1.2em}
.hint.ok{color:#6fe6b2}.hint.warn{color:#f6cd8a}.hint.err{color:#ffa0b2}
/* numbered steps */
.steps{list-style:none;counter-reset:s;padding:0;margin:8px 0 0}
.steps li{counter-increment:s;position:relative;padding:0 0 24px 46px;margin:0}
.steps li:last-child{padding-bottom:0}
.steps li::before{content:counter(s);position:absolute;left:0;top:-2px;width:30px;height:30px;
border-radius:999px;background:var(--grad);color:#fff;font-weight:700;font-size:.85rem;
display:flex;align-items:center;justify-content:center;box-shadow:0 4px 12px -4px rgba(168,85,247,.6)}
.steps li:not(:last-child)::after{content:"";position:absolute;left:14px;top:32px;bottom:6px;
width:2px;background:linear-gradient(var(--line),transparent)}
.steps h5{margin:3px 0 8px;font-size:1rem}
.steps .hint{margin-top:6px}
/* welcome checklist */
.checklist{display:flex;flex-direction:column;gap:9px;margin:18px 0 24px}
.ck{display:flex;gap:13px;align-items:flex-start;padding:14px 16px;border-radius:12px;
border:1px solid var(--line);background:var(--panel);font-size:.92rem;box-shadow:var(--shadow)}
.ck .m{width:23px;height:23px;border-radius:999px;flex:none;display:flex;align-items:center;
justify-content:center;font-size:.78rem;font-weight:700;border:1px solid var(--line);color:var(--faint)}
.ck.done{border-color:var(--okln)}
.ck.done .m{background:var(--grad);color:#fff;border-color:transparent}
.ck .t{flex:1}.ck .t b{display:block;color:var(--fg);font-weight:600}
.ck .t span{color:var(--dim);font-size:.85rem}
.ghwait{opacity:.72}
/* app shell (sidebar) */
.app{display:flex;min-height:100vh;align-items:stretch}
.app::before{content:"";position:fixed;left:0;right:0;top:0;height:2px;background:var(--grad);z-index:40}
.side{width:242px;flex:none;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;
padding:18px 14px;border-right:1px solid var(--hair);background:rgba(12,14,20,.55);gap:4px}
.side .brand{padding:8px 8px 16px}
.nav{display:flex;flex-direction:column;gap:3px;margin-top:4px}
.ni{display:flex;align-items:center;gap:11px;padding:9px 11px;border-radius:10px;color:var(--dim);
font-size:.92rem;font-weight:500;transition:background .14s,color .14s}
.ni svg{width:18px;height:18px;flex:none}
.ni:hover{background:var(--panel);color:var(--fg)}
.ni.on{color:var(--fg);background:linear-gradient(90deg,rgba(168,85,247,.16),rgba(91,124,250,.04));
font-weight:600}.ni.on svg{color:var(--purple)}
.sidefoot{margin-top:auto;display:flex;flex-direction:column;gap:10px;padding:14px 8px 2px;
border-top:1px solid var(--hair)}
.sidefoot .who{display:flex;align-items:center;gap:9px;color:var(--fg);font-weight:550;font-size:.9rem}
.sidefoot .av{width:26px;height:26px;border-radius:50%;background:var(--grad);color:#fff;flex:none;
display:inline-flex;align-items:center;justify-content:center;font-size:.78rem;font-weight:700}
.sidefoot .live{align-self:flex-start;font-size:.66rem;font-weight:700;letter-spacing:.05em;
text-transform:uppercase;padding:3px 9px;border-radius:999px}
.sidefoot .live.on{background:var(--okbg);color:#6fe6b2;border:1px solid var(--okln)}
.sidefoot .live.dry{background:var(--warnbg);color:#f6cd8a;border:1px solid var(--warnln)}
.sidefoot .so{color:var(--dim);font-size:.86rem}.sidefoot .so:hover{color:var(--fg)}
.main{flex:1;min-width:0}
.main .wrap{padding:34px 36px 56px;max-width:58rem;margin:0}
.side{overflow-y:auto}
@media(max-width:820px){.app{flex-direction:column}.app::before{position:sticky}
.side{width:auto;height:auto;flex-direction:row;align-items:center;padding:9px 14px;gap:8px;z-index:30;
background:rgba(10,11,18,.85);backdrop-filter:saturate(140%) blur(14px)}
.side .brand{padding:0 4px 0 0}.nav{flex-direction:row;margin:0;gap:2px}.ni span{display:none}.ni{padding:8px}
.sidefoot{margin:0 0 0 auto;flex-direction:row;align-items:center;border-top:0;padding:0;gap:10px}
.sidefoot .nm,.sidefoot .live{display:none}.main .wrap{padding:22px 16px 0}}
/* integration cards */
.intg{padding:22px 24px;border:1px solid var(--line);border-radius:16px;margin:16px 0;
background:linear-gradient(180deg,rgba(255,255,255,.022),transparent 40%),var(--panel);
box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 12px 34px -16px rgba(0,0,0,.7);
animation:rise .5s cubic-bezier(.2,.7,.2,1) both}
.intg.ok{border-color:rgba(61,220,151,.22)}
.itop{display:flex;gap:15px;align-items:flex-start}
.iico{width:46px;height:46px;border-radius:13px;flex:none;display:flex;align-items:center;
justify-content:center;box-shadow:0 4px 12px -4px rgba(0,0,0,.6)}
.iico.gh{background:#161b22;color:#fff;border:1px solid #2b3140}
.iico.slack{background:#fff}
.iico.claude{background:linear-gradient(135deg,#d97757,#c15f3c);color:#fff}
.imeta{flex:1;min-width:0}
.iname{display:flex;align-items:center;gap:9px;font-weight:650;font-size:1.08rem;letter-spacing:-.01em}
.idesc{color:var(--dim);font-size:.88rem;margin-top:3px}
.ictl{margin-top:16px}
.ictl .hint{margin:9px 0 0}
/* form controls that read as controls */
.in{width:100%;background:#0a0c14;border:1px solid var(--line);border-radius:10px;padding:11px 13px;
font:13.5px/1.4 var(--mono);color:var(--fg);transition:border-color .16s,box-shadow .16s}
.in:focus{outline:0;border-color:var(--purple);box-shadow:0 0 0 3px rgba(168,85,247,.16)}
.inrow{display:flex;gap:9px;align-items:stretch}.inrow .in{flex:1;min-width:0}
.intg .btn{background:#252c40}.intg .btn:hover{background:#2e3650}
.intg .btn.primary{background:#5b56e0}.intg .btn.primary:hover{background:#6a65ec}
.discbtn{background:none;border:0;color:var(--faint);font-size:.83rem;font-weight:600;cursor:pointer;
padding:6px 0;margin-top:8px}.discbtn:hover{color:var(--err)}
.replace{background:none;border:0;color:var(--blue);font-size:.85rem;font-weight:600;cursor:pointer;
padding:0;margin-top:12px}.replace:hover{color:#8aa2ff}
.tag-on{font-size:.68rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:3px 9px;
border-radius:999px;background:var(--okbg);color:#6fe6b2;border:1px solid var(--okln)}
.tag-off{font-size:.68rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:3px 9px;
border-radius:999px;background:var(--panel2);color:var(--faint);border:1px solid var(--line)}
.tag-req{font-size:.68rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:3px 9px;
border-radius:999px;background:var(--warnbg);color:#f6cd8a;border:1px solid var(--warnln)}
.ratebar{height:7px;border-radius:999px;background:var(--panel2);overflow:hidden;margin-top:8px}
.ratefill{height:100%;background:var(--grad);border-radius:999px}
/* how-it-works */
.flow{display:flex;flex-direction:column;gap:0;margin:10px 0 8px}
.fstep{display:flex;gap:16px;padding:0 0 26px}
.fstep:last-child{padding-bottom:0}
.fstep .fn{width:34px;height:34px;border-radius:999px;flex:none;background:var(--grad);color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:700;position:relative;
box-shadow:0 4px 12px -4px rgba(168,85,247,.6)}
.fstep:not(:last-child) .fn::after{content:"";position:absolute;top:34px;left:50%;transform:translateX(-50%);
width:2px;height:calc(100% - 8px);background:linear-gradient(var(--line),transparent)}
.fstep .fb{flex:1;padding-top:4px}.fstep .fb h4{margin:0 0 5px}
.shot{margin:12px 0 0;width:fit-content;max-width:100%;border:1px solid var(--line);
border-radius:10px;overflow:hidden;box-shadow:var(--shadow)}
.shot img{display:block;width:430px;max-width:100%}
.shot .cap{padding:8px 12px;font-size:.8rem;color:var(--faint);border-top:1px solid var(--line2)}
.shot.ph{padding:22px;text-align:center;color:var(--faint);font-size:.85rem;border-style:dashed}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:14px 0}
.mini{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 17px}
.mini .t{font-weight:600;margin-bottom:4px;display:flex;align-items:center;gap:8px}
/* sign-in screen */
.auth{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px 18px;
background-image:radial-gradient(720px 360px at 50% -60px,rgba(168,85,247,.16),transparent 68%),
radial-gradient(620px 320px at 50% 120%,rgba(91,124,250,.12),transparent 70%)}
.authcard{width:100%;max-width:428px;background:linear-gradient(180deg,var(--panel2),var(--panel));
border:1px solid var(--line);border-radius:22px;padding:40px 34px 30px;text-align:center;
box-shadow:0 32px 90px -34px rgba(0,0,0,.85),0 0 0 1px rgba(168,85,247,.05);
animation:rise .55s cubic-bezier(.2,.7,.2,1) both}
.authlogo{width:66px;height:66px;margin:0 auto 16px;display:block;
filter:drop-shadow(0 8px 22px rgba(168,85,247,.45))}
.authcard h1{font-size:1.9rem;letter-spacing:-.02em;margin:0;background:var(--grad);
-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.authsub{color:var(--fg);font-size:.98rem;font-weight:550;margin:6px 0 0}
.authlead{color:var(--dim);font-size:.9rem;line-height:1.6;margin:16px 0 24px}
.authcard form{margin:0}
.authcard .btn.soft.block{margin-top:2px}
.authhint{text-align:center;margin:10px 0 18px}
.tokfield{position:relative;margin-top:4px}
.tokfield input{width:100%;padding:13px 62px 13px 14px;text-align:center;letter-spacing:.02em}
.tokfield input::placeholder{text-align:center}
.peek{position:absolute;right:6px;top:50%;transform:translateY(-50%);background:none;border:0;
color:var(--dim);font-size:.8rem;font-weight:600;cursor:pointer;padding:6px 10px;border-radius:8px}
.peek:hover{color:var(--fg);background:var(--panel2)}
.authfine{font-size:.78rem;color:var(--faint);margin:20px 0 0;line-height:1.5}
.authcard .hint#pathint{text-align:center;margin:9px 0 2px}
/* soft (borderless) button — used on the sign-in screen */
.btn.soft{background:var(--panel2);border-color:transparent;color:var(--fg)}
.btn.soft:hover{background:#20243a;border-color:transparent}
.btn.block{width:100%;justify-content:center;margin-top:6px}
/* animation */
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@keyframes spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media(max-width:640px){.wrap{padding:20px 15px 0}.thread{margin-left:0}
h1{font-size:1.28rem}.topbar{padding:0 15px}}
"""

JS = r"""
function upd(){
  var n=0,b=document.querySelectorAll('.fsel');
  b.forEach(function(c){
    c.closest('.finding').classList.toggle('off',!c.checked);
    if(c.checked)n++;
  });
  var t=document.getElementById('cnt');
  if(t)t.textContent=n;
  var s=document.getElementById('submit');
  if(s)s.disabled=(n===0);
}
function all(v){document.querySelectorAll('.fsel').forEach(function(c){c.checked=v});upd()}
document.addEventListener('change',function(e){if(e.target.classList.contains('fsel'))upd()});
document.addEventListener('DOMContentLoaded',upd);
function tokcheck(){
  var p=document.getElementById('pat'),h=document.getElementById('pathint'),
      g=document.getElementById('go');
  if(!p||!h||!g)return;
  var v=p.value.trim(),ok=false,m='',c='';
  if(!v){m='';}
  else if(/^ghp_[A-Za-z0-9]{36}$/.test(v)){ok=true;c='ok';m='\u2713 Looks like a classic token';}
  else if(/^github_pat_/.test(v)){ok=true;c='warn';m='\u26a0 Fine-grained token \u2014 the org\u2019s approval policy can block these. A classic token is the safe choice.';}
  else if(/^gh[ous]_[A-Za-z0-9]+$/.test(v)){ok=true;c='warn';m='\u26a0 That is an app/OAuth token, not a personal one \u2014 it may work, but a classic ghp_ token is what this expects.';}
  else{c='err';m='That does not look like a GitHub token \u2014 classic ones start with ghp_ and are 40 characters.';}
  h.textContent=m;h.className='hint '+c;g.disabled=!ok;
}
function slackcheck(){
  var s=document.getElementById('slack_id'),h=document.getElementById('slackhint');
  if(!s||!h)return;
  var v=s.value.trim();
  if(!v){h.textContent='';h.className='hint';return;}
  if(/^[UW][A-Z0-9]{8,12}$/.test(v)){h.textContent='\u2713 Looks like a member ID';h.className='hint ok';}
  else if(/^@/.test(v)||/\s/.test(v)){h.textContent='Paste the member ID (starts with U), not your @handle.';h.className='hint err';}
  else{h.textContent='Member IDs start with U and are 9\u201313 characters, e.g. U0123ABCDEF.';h.className='hint warn';}
}
function peek(id,btn){var i=document.getElementById(id);if(!i)return;
  i.type=i.type==='password'?'text':'password';btn.textContent=i.type==='password'?'show':'hide';}
document.addEventListener('input',function(e){
  if(e.target.id==='pat')tokcheck();if(e.target.id==='slack_id')slackcheck();});
document.addEventListener('submit',function(e){
  var b=e.target.querySelector('button[type=submit]');
  if(b){b.dataset.was=b.textContent;
    b.innerHTML='<span class=spin></span>'+(b.dataset.busy||'Working\u2026');
    setTimeout(function(){b.disabled=true;},0);}});
document.addEventListener('DOMContentLoaded',function(){tokcheck();slackcheck();});
"""


NAV_ICONS = {
    "queue": "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.8 "
             "stroke-linecap=round stroke-linejoin=round><path d='M3 5h18M3 12h18M3 19h11'/>"
             "</svg>",
    "integrations": "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.8 "
                    "stroke-linejoin=round><rect x='3' y='3' width='7' height='7' rx='1.5'/>"
                    "<rect x='14' y='3' width='7' height='7' rx='1.5'/>"
                    "<rect x='14' y='14' width='7' height='7' rx='1.5'/>"
                    "<rect x='3' y='14' width='7' height='7' rx='1.5'/></svg>",
    "how": "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.8 "
           "stroke-linecap=round stroke-linejoin=round><circle cx='12' cy='12' r='9'/>"
           "<path d='M9.6 9.2a2.5 2.5 0 1 1 3.4 2.3c-.8.5-1 .9-1 1.7M12 17h.01'/></svg>",
    "learnings": "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.8 "
                 "stroke-linecap=round stroke-linejoin=round><path d='M12 3l2.1 4.5L19 8.2l-3.5"
                 " 3.3.9 4.9L12 14.1 7.6 16.4l.9-4.9L5 8.2l4.9-.7z'/></svg>",
    "skills": "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.8 "
              "stroke-linecap=round stroke-linejoin=round><path d='M16 18l6-6-6-6M8 6l-6 6 6 6'/>"
              "</svg>",
}


def sidebar(user, active):
    items = [("queue", "Queue", "/prbot/"),
             ("learnings", "Learnings", "/prbot/learnings"),
             ("skills", "Skills", "/prbot/skills"),
             ("integrations", "Integrations", "/prbot/integrations"),
             ("how", "How it works", "/prbot/how")]
    nav = "".join(
        f"<a class='ni{' on' if active == k else ''}' href='{href}'>{NAV_ICONS[k]}"
        f"<span>{label}</span></a>" for k, label, href in items)
    av = html.escape((user[:1] or "?").upper())
    return (f"<aside class=side><a class=brand href='/prbot/'>"
            f"<img src='{prbot_assets.LOGO}' alt=''><span class=n>{html.escape(BRAND)}</span></a>"
            f"<nav class=nav>{nav}</nav>"
            f"<div class=sidefoot>"
            f"<span class='live {'dry' if DRY_RUN else 'on'}'>"
            + ("dry run" if DRY_RUN else "live") + "</span>"
            f"<div class=who><span class=av>{av}</span>"
            f"<span class=nm>{html.escape(user)}</span></div>"
            f"<a class=so href='/prbot/logout'>Sign out</a></div></aside>")


def shell(title, body, refresh=None, user=None, active=None, auth=False):
    r = f'<meta http-equiv=refresh content="8;url={refresh}">' if refresh else ""
    head = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)} · {html.escape(BRAND)}</title>"
            f"<link rel=icon href='{prbot_assets.FAVICON}'>"
            f"<link rel=preconnect href='https://fonts.googleapis.com'>"
            f"<link rel=preconnect href='https://fonts.gstatic.com' crossorigin>"
            f"<link rel=stylesheet href='https://fonts.googleapis.com/css2?"
            f"family=Inter:wght@400;500;600;700&display=swap'>"
            f"{r}<style>{CSS}</style></head><body>")
    if auth:                                    # sign-in: full-page centered card, no chrome
        inner = body
    elif user:                                  # signed-in pages get the sidebar app-shell
        inner = (f"<div class=app>{sidebar(user, active)}"
                 f"<main class=main><div class=wrap>{body}</div></main></div>")
    else:                                       # error pages: bare centered layout
        inner = (f"<div class=topbar><a class=brand href='/prbot/'>"
                 f"<img src='{prbot_assets.LOGO}' alt=''>"
                 f"<span class=n>{html.escape(BRAND)}</span>"
                 f"<span class=tag>PR review</span></a></div>"
                 f"<div class=wrap>{body}</div>")
    return head + inner + f"<script>{JS}</script></body></html>"


def pill(kind, text=None):
    return f"<span class='pill {kind}'>{html.escape(text or kind)}</span>"


def dry_banner():
    if not DRY_RUN:
        return ""
    return ("<div class='banner warn'><span>🧪</span><div><b>DRY RUN — the buttons on this "
            "page do not write to GitHub.</b><br>They report what <em>would</em> happen. To "
            "post and approve for real, on the box run "
            "<code>sed -i 's/^DRY_RUN=1/DRY_RUN=0/' ~/.claude-pr-bot/.env</code> then "
            "<code>sudo systemctl restart prbot</code>.</div></div>")


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


def pr_meta(pr):
    """Identity for a PR, from the live queue if still there, else the cached copy.

    A PR leaves queue.json the moment you submit a review or the request moves on — but its
    review is still on disk and still worth reading, so fall back to meta.json.
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
    if st in ("done", "failed", "stalled"):
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

    def deny(self, msg):
        self.reply(403, shell("Rejected", "<h1>Rejected</h1><div class='banner err'><span>🚫"
                                          f"</span><div>{html.escape(msg)}</div></div>"))

    # -- GET ---------------------------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        route = u.path.rstrip("/").removeprefix("/prbot") or "/"
        pr = (q.get("pr") or [""])[0]
        exp = (q.get("exp") or [""])[0]
        sig = (q.get("sig") or [""])[0]

        if route == "/health":
            return self.reply(200, "ok", "text/plain; charset=utf-8")
        if route == "/login":
            return self.login_page((q.get("next") or ["/prbot/"])[0])
        if route == "/logout":
            return self.redirect("/prbot/login", cookie=clear_session_cookie())
        if route == "/oauth/start":
            if not OAUTH_ENABLED:
                return self.login_page("/prbot/", "<div class='banner warn'><span>⚠️</span>"
                                       "<div>GitHub login is not configured on this box yet — "
                                       "use a token below.</div></div>")
            nxt = (q.get("next") or ["/prbot/"])[0]
            return self.redirect(oauth_authorize_url(nxt if nxt.startswith("/prbot/")
                                                     else "/prbot/"))
        if route == "/oauth/callback":
            return self.oauth_callback((q.get("code") or [""])[0],
                                       (q.get("state") or [""])[0],
                                       (q.get("error_description") or
                                        q.get("error") or [""])[0])

        user = session_user(self.headers)
        if not user:
            return self.to_login()
        if route in ("/settings", "/integrations"):
            return self.settings_page(user, welcome=bool((q.get("welcome") or [""])[0]),
                                      nxt=(q.get("next") or ["/prbot/"])[0])
        if route == "/how":
            return self.how_page(user)
        if route == "/learnings":
            return self.learnings_page(user)
        if route == "/skills":
            return self.skills_page(user)
        if route == "/":
            tab = (q.get("tab") or ["todo"])[0]
            srt = (q.get("sort") or ["newest"])[0]
            return self.index_page(user, tab if tab in dict(TABS) else "todo", srt)
        if not pr.isdigit():
            return self.reply(400, shell("Bad request", "<h1>Missing PR number.</h1>"))
        if route == "/pr":
            return self.detail_page(pr, user)
        if route in ("/archive", "/unarchive"):
            if err := verify(route[1:], pr, exp, sig):
                return self.deny(err)
            f = udir(pr, user) / "archived"
            f.parent.mkdir(parents=True, exist_ok=True)
            if route == "/archive":
                f.write_text(str(int(time.time())))
            else:
                f.unlink(missing_ok=True)
            return self.redirect(link("", ""))
        if route == "/review":
            # Slack cards carry a "review" token; the dashboard's Re-run button mints one too.
            if err := verify("review", pr, exp, sig):
                return self.deny(err)
            return self.start_review(pr, user, force=True,
                                     effort=(q.get("effort") or [""])[0])
        return self.reply(404, shell("Not found", "<h1>Not found.</h1>"))

    # -- POST --------------------------------------------------------------------------------
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        route = urlparse(self.path).path.rstrip("/").removeprefix("/prbot")
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        if route == "/login":
            return self.do_login(form)
        user = session_user(self.headers)
        if not user:
            return self.to_login()
        if route in ("/settings", "/integrations"):
            return self.do_settings(user, form)
        if route in ("/claude/start", "/claude/code", "/claude/cancel", "/claude/disconnect"):
            return self.do_claude_connect(user, route.rsplit("/", 1)[1], form)
        if route in ("/skill/save", "/skill/reset", "/skill/rule"):
            return self.do_skill(user, route.rsplit("/", 1)[1], form)
        pr, exp, sig = one("pr"), one("exp"), one("sig")
        if not pr.isdigit():
            return self.reply(400, shell("Bad request", "<h1>Missing PR number.</h1>"))
        action = {"/post": "post", "/approve": "approve",
                  "/markdone": "markdone"}.get(route)
        if not action:
            return self.reply(404, shell("Not found", "<h1>Not found.</h1>"))
        if err := verify(action, pr, exp, sig):
            return self.deny(err)
        if action == "post":
            return self.do_post_comments(pr, user, form)
        if action == "markdone":
            d = udir(pr, user)
            d.mkdir(parents=True, exist_ok=True)
            (d / "approved").write_text(json.dumps(
                {"at": int(time.time()), "manual": True,
                 "body": "Handled outside the bot — approved on GitHub directly."}))
            return self.redirect(link("pr", pr))
        return self.do_approve(pr, user, form)

    # -- auth pages ----------------------------------------------------------------------------
    def oauth_callback(self, code, state, error):
        nxt = oauth_check_state(state)
        if nxt is None:
            return self.login_page("/prbot/", "<div class='banner err'><span>🚫</span><div>"
                                   "That sign-in link was stale or altered — try again.</div>"
                                   "</div>")
        if error or not code:
            return self.login_page(nxt, f"<div class='banner err'><span>🚫</span><div>GitHub "
                                        f"did not complete the sign-in: "
                                        f"{html.escape(error or 'no code returned')}</div></div>")
        d = oauth_token_request({"code": code,
                                 "redirect_uri": f"{PUBLIC_URL}/prbot/oauth/callback"})
        if not d:
            return self.login_page(nxt, "<div class='banner err'><span>🚫</span><div>GitHub "
                                        "rejected the sign-in code. Try again.</div></div>")
        login, name, err = verify_pat(d["access_token"])
        if err:
            # The usual cause is on the org side, not the user's: a GitHub App not yet
            # installed on the org, or an OAuth App blocked by the org's third-party access
            # restrictions. Say that, rather than "bad token".
            OAUTH_BLOCKED.write_text(str(int(time.time())))
            return self.login_page(nxt, (
                f"<div class='banner err'><span>🚫</span><div><b>GitHub signed you in, but the "
                f"token cannot see <code>{html.escape(REPO)}</code>.</b><br>An org owner needs "
                f"to allow this app once: for an OAuth App, approve it under the org's "
                f"<em>Third-party access</em> settings; for a GitHub App, install it on the org. "
                f"Until then, sign in with a token below.</div></div>"))
        OAUTH_BLOCKED.unlink(missing_ok=True)
        prev = load_users().get(login) or {}
        oauth_store(login, d, name, prev)
        print(f"login (github): {login}", flush=True)
        # First time in: finish the two remaining connections before landing in the queue.
        if not prev.get("slack_id"):
            nxt = "/prbot/settings?welcome=1&next=" + quote(nxt, safe="")
        return self.redirect(nxt, cookie=session_cookie(login))

    def login_page(self, nxt="/prbot/", banner=""):
        if not nxt.startswith("/prbot/"):
            nxt = "/prbot/"
        # GitHub's new-token page accepts the scope and name in the URL, so the person only
        # has to pick an expiry and click Generate — no hunting for the right checkbox.
        new_tok = "https://github.com/settings/tokens/new?" + urlencode(
            {"scopes": "repo", "description": f"Robin ({ENV.get('PRBOT_ENV', 'prbot')})"})
        body = (
            "<div class=auth><div class=authcard>"
            f"<img class=authlogo src='{prbot_assets.LOGO}' alt=''>"
            f"<h1>{html.escape(BRAND)}</h1>"
            "<p class=authsub>Your PR reviewer for Cut &amp; Dry</p>"
            "<p class=authlead>Sign in with a GitHub token. Every comment and approval posts "
            "under your own name — nothing is ever posted for you.</p>"
            + banner
            + f"<a class='btn soft block' target=_blank rel=noopener href='{new_tok}'>"
              "Create a token on GitHub ↗</a>"
              "<div class='hint authhint'>Opens GitHub with the <code>repo</code> scope "
              "pre-filled — pick an expiry, <b>Generate token</b>, copy it.</div>"
              "<form method=post action='/prbot/login'>"
              "<div class=tokfield><input type=password id=pat name=pat placeholder='ghp_…' "
              "autocomplete=off spellcheck=false autofocus required>"
              "<button type=button class=peek onclick=\"peek('pat',this)\">show</button></div>"
              "<div class='hint' id=pathint></div>"
              f"<input type=hidden name=next value='{html.escape(nxt)}'>"
              "<button class='btn primary block' id=go type=submit "
              "data-busy='Signing in…' disabled>Sign in</button>"
              "</form>"
              "<p class=authfine>Stored encrypted on this box, used only to post the comments "
              "and approvals you pick. Revoke it any time on GitHub.</p>"
              "</div></div>")
        return self.reply(200, shell("Sign in", body, auth=True))

    def do_login(self, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        pat, nxt = one("pat").strip(), one("next")
        if not pat:
            return self.login_page(nxt, "<div class='banner warn'><span>⚠️</span><div>Paste "
                                        "a token.</div></div>")
        login, name, err = verify_pat(pat)
        if err:
            return self.login_page(nxt, f"<div class='banner err'><span>🚫</span><div>"
                                        f"{html.escape(err)}</div></div>")
        users = load_users()
        prev = users.get(login) or {}
        u = dict(prev)
        u.update({"pat_enc": enc(pat), "name": name,
                  "added": prev.get("added") or int(time.time()), "updated": int(time.time())})
        users[login] = u
        save_users(users)
        print(f"login: {login}", flush=True)
        if not nxt.startswith("/prbot/"):
            nxt = "/prbot/"
        # First time in: finish the two remaining connections before landing in the queue.
        if not prev.get("slack_id"):
            nxt = "/prbot/settings?welcome=1&next=" + quote(nxt, safe="")
        return self.redirect(nxt, cookie=session_cookie(login))

    def settings_page(self, user, banner="", welcome=False, nxt="/prbot/"):
        u = load_users().get(user) or {}
        exp, sig = mint("settings", user, ACTION_TTL)
        if not nxt.startswith("/prbot/"):
            nxt = "/prbot/"
        has_slack, has_claude = bool(u.get("slack_id")), bool(u.get("claude_token_enc"))
        claude_inner, claude_aux = self.claude_section(user, u, exp, sig, welcome, nxt)
        hidden = (f"<input type=hidden name=exp value='{exp}'>"
                  f"<input type=hidden name=sig value='{sig}'>"
                  f"<input type=hidden name=welcome value='{'1' if welcome else ''}'>"
                  f"<input type=hidden name=next value='{html.escape(nxt)}'>")
        on = "<span class=tag-on>Connected</span>"
        off = "<span class=tag-off>Not connected</span>"
        req = "<span class=tag-req>Required</span>"

        def card(icon_cls, icon, name, chip, sub, control, ok=False):
            return (f"<div class='intg{' ok' if ok else ''}'><div class=itop>"
                    f"<div class='iico {icon_cls}'>{icon}</div><div class=imeta>"
                    f"<div class=iname>{name} {chip}</div><div class=idesc>{sub}</div></div></div>"
                    f"<div class=ictl>{control}</div></div>")

        gh_ctl = ("<button type=button class=replace onclick=\"this.hidden=true;"
                  "document.getElementById('gh-rep').hidden=false\">Replace token</button>"
                  "<div id=gh-rep hidden><form method=post action='/prbot/settings'>"
                  "<div class=inrow><input type=password class=in name=pat placeholder='ghp_…' "
                  "autocomplete=off spellcheck=false>"
                  "<button class='btn primary' type=submit data-busy='Saving…'>Save</button></div>"
                  + hidden + "</form></div>")
        github_card = card("gh", GH_ICON, "GitHub", on,
                           f"Connected as <code>{html.escape(user)}</code> — comments and "
                           "approvals post under your name.", gh_ctl, ok=True)

        slack_ctl = ("<form method=post action='/prbot/settings'>"
                     "<div class=inrow><input type=text class=in id=slack_id name=slack_id "
                     f"placeholder='U0123ABCDEF' value='{html.escape(u.get('slack_id', ''))}' "
                     "autocomplete=off spellcheck=false>"
                     "<button class='btn primary' type=submit data-busy='Saving…'>Save</button>"
                     "</div><div class=hint id=slackhint></div>"
                     "<div class=hint>In Slack: your <b>profile picture</b> → <b>Profile</b> → "
                     "the <b>⋮</b> menu → <b>Copy member ID</b>.</div>" + hidden + "</form>")
        slack_card = card("slack", SLACK_ICON, "Slack", on if has_slack else off,
                          "Pings you when a review is requested.", slack_ctl, ok=has_slack)

        claude_card = card("claude", CLAUDE_ICON, "Claude", on if has_claude else req,
                           "Reviews you start run on your own Claude account.",
                           claude_inner, ok=has_claude) + claude_aux

        my_skill = user_skill(user)
        skill_svg = ("<svg viewBox='0 0 24 24' width=22 height=22 fill=none stroke=currentColor "
                     "stroke-width=1.8 stroke-linecap=round stroke-linejoin=round>"
                     "<path d='M16 18l6-6-6-6M8 6l-6 6 6 6'/></svg>")
        skill_ctl = self.skill_control(user, exp, sig)
        skill_chip = ("<span class=tag-on>Custom</span>" if my_skill
                      else "<span class=tag-off>Global default</span>")
        skill_card = card("skill", skill_svg, "Review skill", skill_chip,
                          "The reviewing approach your reviews use. "
                          "<a href='/prbot/skills'>See how each skill scores →</a>",
                          skill_ctl, ok=bool(my_skill))

        cards = github_card + slack_card + claude_card + skill_card
        if welcome:
            ready = has_claude and has_slack
            cta = (f"<a class='btn primary block' href='{html.escape(nxt)}'>Go to your queue →"
                   "</a>" if ready else
                   "<button class='btn primary block' disabled>Connect Claude & Slack to "
                   "continue</button>")
            body = (f"<h1>Welcome to {html.escape(BRAND)}, {html.escape(u.get('name') or user)}</h1>"
                    "<p class=lead>You're signed in with GitHub. Connect Slack and your Claude "
                    "account to finish — then you're ready to review.</p>" + banner + cards
                    + f"<div style='margin-top:20px'>{cta}</div>")
            return self.reply(200, shell("Set up Robin", body, user=user, active="integrations"))

        body = (f"<h1>Integrations</h1>"
                "<p class=lead>The services Robin connects to. Everything is stored encrypted "
                "on this box and used only on your behalf.</p>" + banner + cards)
        return self.reply(200, shell("Integrations", body, user=user, active="integrations"))

    def how_page(self, user):
        def fstep(n, title, txt, img=None):
            s = (f"<div class=shot><img src='{prbot_howimg.IMG[img]}' alt='' loading=lazy></div>"
                 if img else "")
            return (f"<div class=fstep><div class=fn>{n}</div><div class=fb>"
                    f"<h4>{title}</h4><div class='muted sm'>{txt}</div>{s}</div></div>")
        flow = ("<div class=flow>"
                + fstep(1, "A review is requested",
                        "A teammate adds you as a reviewer on a PR. Robin notices within 3 "
                        "minutes and sends you a Slack card — no need to watch GitHub.",
                        "slack")
                + fstep(2, "Robin drafts the review",
                        "You click through to the PR. Robin checks out the branch and runs the "
                        "<code>pr-review</code> skill against the real diff — about 10–15 "
                        "minutes for a 25-file PR. Nothing is posted to GitHub in this step.",
                        "progress")
                + fstep(3, "You decide what's worth saying",
                        "The findings appear as editable cards, each with a severity and a "
                        "<code>file:line</code>. Tick the ones you agree with, edit any wording, "
                        "ignore the rest.",
                        "findings")
                + fstep(4, "Post — as you",
                        "The selected comments post to the PR as inline review comments under "
                        "your own GitHub name. Always a plain COMMENT review — Robin never "
                        "requests changes or blocks a merge.",
                        "post")
                + fstep(5, "Approve when you're ready",
                        "A separate click posts an LGTM comment and approves the PR, also as "
                        "you. This is what stops the 'posted comments, forgot to approve' loop.",
                        "approve")
                + "</div>")
        guarantees = ("<div class=grid2>"
                      "<div class=mini><div class=t>🧑 Always you</div><div class='muted sm'>"
                      "Every comment and approval posts under your own GitHub account.</div></div>"
                      "<div class=mini><div class=t>🖱️ Nothing automatic</div>"
                      "<div class='muted sm'>Nothing reaches GitHub without your click. The "
                      "review step can't write to GitHub at all.</div></div>"
                      "<div class=mini><div class=t>💬 Comments, not blocks</div>"
                      "<div class='muted sm'>Robin posts plain review comments — it never "
                      "requests changes or blocks a merge.</div></div>"
                      "<div class=mini><div class=t>🔒 Your credentials, encrypted</div>"
                      "<div class='muted sm'>Your GitHub and Claude tokens are encrypted on the "
                      "box and used only for your actions.</div></div></div>")
        tabs = "".join(
            f"<div class=mini><div class=t>{lbl}</div><div class='muted sm'>"
            f"{TAB_DESC.get(k, '')}</div></div>" for k, lbl in TABS if k != "all")
        body = (f"<h1>How {html.escape(BRAND)} works</h1>"
                "<p class=lead>Robin drafts the PR reviews you owe your team, and lets you send "
                "them with a click. It's an assistant — you stay the reviewer.</p>"
                + flow
                + "<h2>The guarantees</h2>" + guarantees
                + "<h2>What the tabs mean</h2><div class=grid2>" + tabs + "</div>"
                + "<p class=fine>Questions or something not working? Ping "
                  f"<code>{html.escape(REVIEWER)}</code>.</p>")
        return self.reply(200, shell("How it works", body, user=user, active="how"))

    def learnings_page(self, user):
        c = prbot_learn.counts()
        rows = prbot_learn.recent(80)
        strip = ("<div class=stats>"
                 f"<a class='stat hot'><div class=k>{c['dropped']}</div>"
                 "<div class=l>Dropped as noise</div></a>"
                 f"<a class=stat><div class=k>{c['edited']}</div><div class=l>Reworded</div></a>"
                 f"<a class=stat><div class=k>{c['kept']}</div>"
                 "<div class=l>Kept as-is</div></a></div>")
        body = (f"<h1>What {html.escape(BRAND)} has learned</h1>"
                "<p class=lead>Every time you drop a finding as noise or reword one before "
                "posting, Robin remembers it and weighs it on the next review of this repo — "
                "so it stops repeating what you reject. This is that memory.</p>" + strip)
        if not rows:
            body += ("<div class=empty><span class=ic>🧠</span><b>Nothing learned yet</b>"
                     "Post or drop a few findings and they'll show up here.</div>")
            return self.reply(200, shell("Learnings", body, user=user, active="learnings"))

        pillmap = {"dropped": ("blocker", "dropped"), "edited": ("should-fix", "reworded"),
                   "kept": ("posted", "kept")}
        items = []
        for r in rows:
            pk, plabel = pillmap.get(r.get("outcome"), ("archived", r.get("outcome", "")))
            loc = html.escape(r.get("path", "") + (f":{r['line']}" if r.get("line") else ""))
            extra = ""
            if r.get("outcome") == "edited" and r.get("edited_gist"):
                extra = (f"<div class='muted sm' style='margin-top:4px'>→ "
                         f"{html.escape(r['edited_gist'])}</div>")
            items.append(
                f"<div class=row><div class=rowlink>"
                f"<div class=rowtop>{pill(pk, plabel)}"
                f"<span class=loc>{loc}</span>{pill(r.get('severity', 'nit'))}</div>"
                f"<div class='muted sm' style='margin-top:5px'>{html.escape(r.get('gist', ''))}"
                f"</div>{extra}</div></div>")
        body += ("<h2>Recent decisions</h2><div class=list>" + "".join(items) + "</div>"
                 "<p class=fine>Shared across the team for this repo. These are preferences, "
                 "not hard rules — Robin still raises a genuine higher-severity issue even if "
                 "it resembles a past drop.</p>")
        return self.reply(200, shell("Learnings", body, user=user, active="learnings"))

    def claude_section(self, user, u, exp, sig, welcome, nxt):
        """(inner, aux): the Claude block for inside the settings <form>, plus the auxiliary
        <form>s its buttons post to. Nested forms are invalid HTML — browsers drop the inner
        tag — so the aux forms are emitted AFTER the main form and the buttons reference them
        by id via the `form=` attribute."""
        hidden = (f"<input type=hidden name=exp value='{exp}'>"
                  f"<input type=hidden name=sig value='{sig}'>"
                  f"<input type=hidden name=welcome value='{'1' if welcome else ''}'>"
                  f"<input type=hidden name=next value='{html.escape(nxt)}'>")
        if u.get("claude_token_enc"):
            return ((f"<div class=hint ok>✓ Connected {fmt_date(u.get('claude_added', 0))} — "
                     "reviews you start run on your own Claude account.</div>"
                     "<button class=discbtn type=submit form=claudedisc>Disconnect</button>"),
                    f"<form id=claudedisc method=post action='/prbot/claude/disconnect'>"
                    f"{hidden}</form>")
        # Not connected: mint the authorize URL now so the single "Connect with Claude" button
        # opens Claude in a new tab (a real user gesture) AND reveals the paste-code field — no
        # intermediate "start" click, no separate "open to authorize" step.
        url, err = claude_connect_start(user)
        if err:
            return (f"<div class=hint err>{html.escape(err)}</div>", "")
        reveal = ("document.getElementById('cc-code').hidden=false;"
                  "this.querySelector('.lbl').textContent='Reopen Claude';"
                  "setTimeout(function(){var i=document.getElementById('cc-input');"
                  "if(i)i.focus();},200)")
        return (
            f"<a class='btn primary block' target=_blank rel=noopener href='{html.escape(url)}' "
            f"onclick=\"{reveal}\">{CLAUDE_ICON}<span class=lbl>Connect with Claude</span></a>"
            "<div class=hint>Opens Claude in a new tab — sign in with <em>your</em> account and "
            "click <b>Authorize</b>. Claude shows you a code; paste it below.</div>"
            "<div id=cc-code hidden style='margin-top:12px'><div class=inrow>"
            "<input type=text class=in id=cc-input name=code form=claudecode "
            "placeholder='paste the code from Claude' autocomplete=off spellcheck=false>"
            "<button class='btn primary' type=submit form=claudecode "
            "data-busy='Connecting…'>Connect</button></div></div>",
            f"<form id=claudecode method=post action='/prbot/claude/code'>{hidden}</form>")

    def do_claude_connect(self, user, step, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        if err := verify("settings", user, one("exp"), one("sig")):
            return self.deny(err)
        welcome, nxt = bool(one("welcome")), one("next") or "/prbot/"
        back = lambda banner="": self.settings_page(user, banner, welcome=welcome, nxt=nxt)  # noqa
        if step == "cancel":
            claude_connect_cancel(user)
            return back()
        if step == "disconnect":
            users = load_users()
            uu = users.get(user) or {}
            for k in ("claude_token_enc", "claude_refresh_enc", "claude_exp", "claude_added"):
                uu.pop(k, None)
            users[user] = uu
            save_users(users)
            claude_connect_cancel(user)
            return back("<div class='banner ok'><span>✓</span><div>Claude disconnected — reviews "
                        "you start use the shared team runner again.</div></div>")
        if step == "start":
            url, err = claude_connect_start(user)
            if err:
                return back(f"<div class='banner err'><span>🚫</span><div>{html.escape(err)}"
                            f"</div></div>")
            return back()
        code = one("code").strip()
        if not code:
            return back("<div class='banner warn'><span>⚠️</span><div>Paste the code Claude "
                        "showed you.</div></div>")
        result, err = claude_connect_code(user, code)
        if err:
            return back(f"<div class='banner err'><span>🚫</span><div>{html.escape(err)}"
                        f"</div></div>")
        ok, why = verify_claude_token(result["access_token"])
        if not ok:
            return back(f"<div class='banner err'><span>🚫</span><div>Got a token from Claude "
                        f"but it did not work here: <code>{html.escape(why)}</code></div></div>")
        store_claude_token(user, result)
        print(f"claude connected: {user}", flush=True)
        if welcome and (load_users().get(user) or {}).get("slack_id"):
            return self.redirect(nxt if nxt.startswith("/prbot/") else "/prbot/")
        return back("<div class='banner ok'><span>✓</span><div>Claude connected — reviews you "
                    "start now run on your own account.</div></div>")

    def do_skill(self, user, step, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        if err := verify("settings", user, one("exp"), one("sig")):
            return self.deny(err)
        # Which skill this edits: the team default (shared) or the user's own.
        target = "global" if one("target") == "global" else user
        who = ("the team default skill" if target == "global" else "your skill")
        # Return to the page the form came from.
        back = ((lambda b="": self.skills_page(user, b)) if one("from") == "skills"
                else (lambda b="": self.settings_page(user, b)))
        ok = lambda m: back(f"<div class='banner ok'><span>✓</span><div>{m}</div></div>")  # noqa

        if step == "rule":
            rule = one("rule")
            if not rule.strip():
                return back("<div class='banner err'><span>🚫</span><div>Type a rule to add."
                            "</div></div>")
            new_text = add_skill_rule(read_skill(target), rule)
            if len(new_text) > 40000:
                return back("<div class='banner err'><span>🚫</span><div>That skill is already "
                            "very large (>40k chars). Trim it before adding more.</div></div>")
            save_skill(target, new_text)
            print(f"skill rule added to {target}: {tidy_rule(rule)!r}", flush=True)
            return ok(f"Added to {who} — Robin will apply it on every review: "
                      f"<b>{html.escape(tidy_rule(rule))}</b>")

        if step == "reset":
            save_skill(target, "")
            return ok("Reset " + ("the team default to the installed skill."
                                  if target == "global" else "to the team default skill."))
        text = one("skill")
        if not text.strip():
            save_skill(target, "")
            return ok("Cleared — " + ("using the installed default skill."
                                      if target == "global" else "using the team default."))
        if len(text) > 40000:
            return back("<div class='banner err'><span>🚫</span><div>That skill is very large "
                        "(>40k chars). Trim it and try again.</div></div>")
        save_skill(target, text)
        print(f"skill saved: {target} ({len(text)} chars)", flush=True)
        return ok(f"Saved {who} — reviews now use it (with Robin's output format appended).")

    def skill_control(self, user, exp, sig, target=None, frm=""):
        """Paste/save/reset + quick-add-rule for one skill. `target` is the skill key: the user's
        login for a personal skill, or "global" for the editable team default. Shown on
        Integrations (personal only) and the Skills page (both)."""
        key = target or user
        is_global = (key == "global")
        cur = read_skill(key)
        tval = "global" if is_global else "me"
        hidden = (f"<input type=hidden name=exp value='{exp}'>"
                  f"<input type=hidden name=sig value='{sig}'>"
                  f"<input type=hidden name=target value='{tval}'>"
                  + (f"<input type=hidden name=from value='{html.escape(frm)}'>" if frm else ""))
        rid = f"skillreset_{'g' if is_global else 'u'}"
        ph = ("Paste the team's pr-review SKILL.md — or leave blank to use the installed default."
              if is_global else
              "Paste your pr-review SKILL.md here — or leave blank for the team default.")
        foot = ("Everyone who hasn't set their own skill uses this. Robin always appends its "
                "output format." if is_global else
                "Your skill's <em>logic</em> runs; Robin always appends its output format, so any "
                "review skill works. Reviews others start are unaffected.")
        reset_label = "Reset to installed default" if is_global else "Reset to team default"
        save_form = (
            "<div class=hint>Load a local skill onto the clipboard, then paste it here:<br>"
            "<code>cat ~/.claude/skills/pr-review/SKILL.md | pbcopy</code> (macOS) · "
            "<code>… | xclip -selection clipboard</code> or <code>… | wl-copy</code> (Linux)."
            "</div>"
            "<form method=post action='/prbot/skill/save'>"
            "<textarea class=in name=skill spellcheck=false style='min-height:130px;"
            f"font-size:12.5px;margin-top:8px' placeholder='{ph}'>{html.escape(cur)}</textarea>"
            f"<div class=hint>{foot}</div>" + hidden
            + "<div class=inrow style='margin-top:10px'>"
              "<button class='btn primary' type=submit data-busy='Saving…'>Save skill</button>"
            + (f"<button class='btn soft' type=submit form={rid}>{reset_label}</button>"
               if cur else "") + "</div></form>"
            + (f"<form id={rid} method=post action='/prbot/skill/reset'>{hidden}</form>"
               if cur else ""))
        # Quick-add: a plain-English rule Robin tidies into the skill's "Team rules" section.
        rule_form = (
            "<div class=rulebox><div class=rule-lbl>Quick-add a rule</div>"
            "<form method=post action='/prbot/skill/rule' class=rulerow>"
            "<input class=in name=rule autocomplete=off placeholder='e.g. Don’t ask for a Jira "
            "ticket link in code comments'>" + hidden
            + "<button class='btn soft' type=submit data-busy='Adding…'>Add rule</button>"
              "</form><div class=hint>Type a preference in plain words — Robin tidies it into "
              "the skill so you don't have to edit the whole file.</div></div>")
        return save_form + rule_form

    def skills_page(self, user, banner=""):
        exp, sig = mint("settings", user, ACTION_TTL)
        my_skill = user_skill(user)
        has_global = bool(read_skill("global"))
        team_card = (
            "<div class=card style='margin-bottom:6px'>"
            "<h4 style='margin-top:0'>Team default skill "
            + ("<span class=tag-on style='margin-left:6px'>Edited</span>" if has_global
               else "<span class=tag-off style='margin-left:6px'>Installed default</span>")
            + "</h4><p class='muted sm' style='margin-top:0'>The shared skill everyone falls "
            "back to. Editing this changes reviews for everyone without their own skill.</p>"
            + self.skill_control(user, exp, sig, target="global", frm="skills") + "</div>")
        skill_card = (
            "<div class=card style='margin-bottom:6px'>"
            "<h4 style='margin-top:0'>Your review skill "
            + ("<span class=tag-on style='margin-left:6px'>Custom</span>" if my_skill
               else "<span class=tag-off style='margin-left:6px'>Team default</span>")
            + "</h4>" + self.skill_control(user, exp, sig, frm="skills") + "</div>")
        stats = prbot_learn.skill_stats()
        head = (f"<h1>Review skills</h1>"
                "<p class=lead>The skill is the reviewing approach Robin follows. Edit the shared "
                "team default or bring your own, and see how each scores by how often its findings "
                "are kept vs dropped as noise.</p>"
                + banner + team_card + skill_card + "<h2>How each skill scores</h2>")
        if not stats:
            body = head + ("<div class=empty><span class=ic>🧭</span><b>No scores yet</b>Post a "
                           "few reviews and each skill's kept-rate will show up here.</div>")
            return self.reply(200, shell("Skills", body, user=user, active="skills"))
        rows = []
        for d in stats:
            label = ("Team default" if d["skill"] == "global"
                     else f"{html.escape(d['skill'])}'s skill")
            you = " <span class=tag-on style='margin-left:6px'>you</span>" if d["skill"] == user else ""
            rate = d["rate"]
            bar = (f"<div class=ratebar><div class=ratefill style='width:{rate}%'></div></div>")
            rows.append(
                f"<div class=row><div class=rowlink><div class=rowtop>"
                f"<span class=ttl>{label}{you}</span>"
                f"<span class=num style='-webkit-text-fill-color:var(--fg)'>{rate}% kept</span>"
                f"</div>{bar}<div class='muted sm' style='margin-top:6px'>"
                f"{d['kept']} kept · {d['edited']} reworded · {d['dropped']} dropped · "
                f"{d['total']} findings</div></div></div>")
        body = (head + "<div class=list>" + "".join(rows) + "</div>"
                "<p class=fine>Kept-rate = findings posted or reworded, over all findings that "
                "skill produced. Higher means the approach matches what reviewers actually want "
                "to say. This is the signal for improving the global skill.</p>")
        return self.reply(200, shell("Skills", body, user=user, active="skills"))

    def do_settings(self, user, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        if err := verify("settings", user, one("exp"), one("sig")):
            return self.deny(err)
        welcome, nxt = bool(one("welcome")), one("next") or "/prbot/"

        def again(msg):
            return self.settings_page(user, f"<div class='banner err'><span>🚫</span><div>{msg}"
                                            f"</div></div>", welcome=welcome, nxt=nxt)
        users = load_users()
        u = users.get(user) or {}
        # Each integration card is its own form, so only touch a field the form actually sent —
        # the GitHub form has no slack_id and must not wipe it, and vice versa.
        if "slack_id" in form:
            u["slack_id"] = one("slack_id").strip()
        pat = one("pat").strip()
        if pat:
            login, name, err = verify_pat(pat)
            if err:
                return again(html.escape(err))
            if login != user:
                return again(f"That token belongs to <code>{html.escape(login)}</code>, not you.")
            u["pat_enc"], u["name"] = enc(pat), name
        u["updated"] = int(time.time())
        users[user] = u
        save_users(users)
        return self.settings_page(user, "<div class='banner ok'><span>✓</span><div>Saved."
                                        "</div></div>", welcome=welcome, nxt=nxt)

    # -- pages -------------------------------------------------------------------------------
    def index_page(self, user, tab="todo", sort="newest"):
        entries = []
        for item, active in all_prs(user):
            num = str(item.get("number"))
            st = pr_state(num, user)
            rev = load_review(num)
            t = pr_times(num, user)
            cs = sev_counts(rev.get("comments", [])) if rev else {}
            entries.append({
                "num": num, "item": item, "active": active, "st": st, "rev": rev,
                "t": t, "cs": cs,
                "updated": iso_ts(item.get("updatedAt")) or iso_ts(item.get("createdAt")),
                "touched": max(t["approved"], t["posted"], t["reviewed"],
                               iso_ts(item.get("updatedAt"))),
                "blockers": cs.get("blocker", 0), "total": sum(cs.values()),
            })

        counts = {k: 0 for k, _ in TABS}
        for e in entries:
            counts[tab_of(e["st"])] += 1
            counts["all"] += 1
        shown = [e for e in entries
                 if (tab_of(e["st"]) == tab
                     or (tab == "all" and e["st"] != "archived"))]

        keys = {
            "newest": lambda e: -(e["updated"] or int(e["num"])),
            "oldest": lambda e: (e["updated"] or int(e["num"])),
            "activity": lambda e: -e["touched"],
            "findings": lambda e: (-e["blockers"], -e["total"]),
        }
        shown.sort(key=keys.get(sort, keys["newest"]))

        def q(**kw):
            return "/prbot/?" + "&".join(f"{k}={v}" for k, v in kw.items())

        tabs = "".join(
            f"<a class='tab{' on' if tab == k else ''}' href='{q(tab=k, sort=sort)}'>"
            f"{lbl}<span class=cnt>{counts[k]}</span></a>" for k, lbl in TABS)
        sorts = "".join(
            f"<a class='sortopt{' on' if sort == k else ''}' href='{q(tab=tab, sort=k)}'>"
            f"{lbl}</a>" for k, lbl in [("newest", "Newest"), ("oldest", "Oldest"),
                                       ("activity", "Recent activity"),
                                       ("findings", "Most findings")])

        rows = []
        for e in shown:
            num, item, t = e["num"], e["item"], e["t"]
            chips = "".join(pill(k, f"{n} {SEV_LABEL.get(k, k)}")
                            for k, n in sorted(e["cs"].items(),
                                               key=lambda kv: SEV_ORDER.get(kv[0], 9)))
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
            size = ""
            if item.get("changedFiles"):
                size = (f"+{item.get('additions', 0):,} −{item.get('deletions', 0):,} · "
                        f"{item['changedFiles']} files")
            act = (f"<a class=rowact href='{link('unarchive', num)}' "
                   f"title='Move back to the queue'>restore</a>"
                   if e["st"] == "archived" else
                   f"<a class=rowact href='{link('archive', num)}' "
                   f"title='Hide from the queue'>archive</a>")
            rows.append(
                f"<div class=row><a class=rowlink href='{link('pr', num)}'>"
                f"<div class=rowtop><span class=num>#{num}</span>"
                f"<span class=ttl>{html.escape(item.get('title', ''))}</span></div>"
                f"<div class='muted sm rowsub'>"
                f"<span>{html.escape(item.get('author', ''))}</span>"
                + (f"<span>{size}</span>" if size else "")
                + "".join(f"<span>{html.escape(w)}</span>" for w in when)
                + (f"<span class=chipwrap>{chips}</span>" if chips else "")
                + "</div></a>"
                + f"<div class=rowmeta>{pill(e['st'])}{act}"
                + f"<a class=chev href='{link('pr', num)}' aria-hidden=true>›</a></div></div>")

        empty = {
            "todo": ("🎉", "You're all caught up", "No PRs are waiting on your review."),
            "reviewed": ("📝", "Nothing to post", "Reviews you've run and not yet posted show here."),
            "posted": ("💬", "Nothing pending approval", "PRs you've commented on but not approved."),
            "approved": ("✅", "Nothing approved yet", "PRs you approve will be listed here."),
            "archived": ("🗂️", "No archived PRs", "Archived PRs are hidden from your working set."),
        }.get(tab, ("📭", "Nothing here yet", "This view is empty."))

        # Stat strip — the three that matter, each a shortcut to its tab.
        def stat(k, label, hot=False):
            cls = "stat hot" if hot else "stat"
            if tab == k:
                cls += " on"
            return (f"<a class='{cls}' href='{q(tab=k, sort=sort)}'>"
                    f"<div class=k>{counts[k]}</div><div class=l>{label}</div></a>")
        strip = ("<div class=stats>"
                 + stat("todo", "Awaiting your review", hot=True)
                 + stat("reviewed", "Ready to post")
                 + stat("posted", "Pending approval")
                 + stat("approved", "Approved") + "</div>")

        slack_ok = bool((load_users().get(user) or {}).get("slack_id"))
        body = (f"<h1>Your review queue</h1>"
                f"<p class='muted sm'>Reviews requested from you across "
                f"<code>{html.escape(REPO)}</code>. Nothing reaches GitHub without your click."
                f"</p>{dry_banner()}"
                + ("" if slack_ok else
                   "<div class='banner warn'><span>💬</span><div>No Slack member ID yet — "
                   "review requests won't ping you. <a href='/prbot/integrations'>Add it in "
                   "Integrations.</a></div></div>")
                + strip
                + f"<div class=tabs>{tabs}</div>"
                + f"<div class=tabdesc>{TAB_DESC.get(tab, '')}</div>"
                + f"<div class=sortbar><span class='muted sm'>Sort</span>{sorts}</div>"
                + "<div class=list>"
                + ("".join(rows) if rows else
                   f"<div class=empty><span class=ic>{empty[0]}</span>"
                   f"<b>{empty[1]}</b>{empty[2]}</div>")
                + "</div>")
        return self.reply(200, shell("Queue", body, user=user, active="queue"))

    def timeline(self, pr, user):
        """reviewed → posted → approved, with dates. Reads at a glance where a PR stands."""
        t = pr_times(pr, user)
        posted = marker(pr, "posted.json", user)
        steps = [
            ("Reviewed", t["reviewed"], ago(t["reviewed"])),
            ("Comments posted", t["posted"],
             f"{posted.get('inline', 0)} inline · {fmt_date(t['posted'])}" if t["posted"] else ""),
            ("Approved", t["approved"], fmt_date(t["approved"])),
        ]
        out = []
        for label, ts, note in steps:
            cls = "step hit" if ts else "step"
            mark = "✓" if ts else "○"
            out.append(f"<span class='{cls}'>{mark} {label}"
                       + (f" <span class=muted>{html.escape(note)}</span>" if note else "")
                       + "</span>")
        return f"<div class=timeline>{''.join(out)}</div>"

    def detail_page(self, pr, user, banner=""):
        # Opening a PR here is what keeps it in your list after it leaves the live queue.
        touch_user(pr, user)
        st = pr_state(pr, user)
        meta, active = pr_meta(pr)
        title = meta.get("title", f"PR #{pr}")
        ghurl = meta.get("url", f"https://github.com/{REPO}/pull/{pr}")
        size = ""
        if meta.get("changedFiles"):
            size = (f" · +{meta.get('additions', 0):,} −{meta.get('deletions', 0):,} · "
                    f"{meta['changedFiles']} files")
        runner_f = STATE / pr / "runner"
        if runner_f.exists():
            who = runner_f.read_text().strip()
            banner += ("<p class='muted sm'>Reviewed on "
                       + (f"<code>{html.escape(who)}</code>'s Claude account"
                          if who and who != "shared" else "the shared team runner")
                       + ".</p>")
        # Domain-risk context — money / catalog / DP paths. Context for the human, never a gate.
        for f in review_risk(pr):
            ic, rttl, note = RISK_INFO[f]
            banner += (f"<div class='banner info'><span>{ic}</span><div><b>{rttl}.</b> {note}"
                       "</div></div>")
        eff = review_effort(pr)
        eff_badge = (f"<span class=effbadge title='Reviewed at {EFFORT[eff][0]} effort — "
                     f"{EFFORT[eff][2]}'>{EFFORT[eff][0]} review</span>"
                     if eff and st not in ("reviewing", "queued") else "")
        # Stale check: the author pushed new commits since this review ran. Flag it (don't
        # auto-re-run — that would spend tokens without a click).
        head_f = STATE / pr / "head"
        cur_head = meta.get("head", "")
        if head_f.exists() and cur_head and head_f.read_text().strip() != cur_head:
            banner += (
                "<div class='banner warn'><span>🔄</span><div><b>The author pushed new commits "
                "since this review.</b> The findings may be out of date — "
                f"<a href='{link('review', pr)}'>re-run the review</a> to check the latest "
                "code.</div></div>")
        head = (f"<nav class=bc><a href='{link('', '')}'>Queue</a><span class=sep>/</span>"
                f"<span class=cur>#{pr}</span></nav>"
                f"<h1 class=prtitle>#{pr} — {html.escape(title)}</h1>"
                f"<div class=meta>{pill(st)}"
                + (pill("dry", "dry run") if DRY_RUN else "") + eff_badge
                + f"<span>{html.escape(meta.get('author', ''))}{size}</span>"
                  f"<span>·</span><a href='{ghurl}'>open on GitHub</a>"
                + ("" if active and user in requested_of(meta)
                   else "<span>· not awaiting your review</span>")
                + "</div>" + self.timeline(pr, user) + banner)

        if st == "stalled":
            was = (STATE / pr / "status").read_text().strip()
            log = (STATE / pr / "agent.log")
            tail = log.read_text()[-400:].strip() if log.exists() else ""
            return self.reply(200, shell(f"#{pr}", head + (
                "<div class='banner err'><span>🔴</span><div><b>The review stopped before it "
                f"finished.</b><br>It was at <code>{html.escape(was)}</code> and nothing is "
                "running now. Re-run it below."
                + (f"<br>Agent output: <code>{html.escape(tail)}</code>" if tail else "")
                + "</div></div>"
                f"<div class=card>{self.effort_picker(pr, meta, label='Re-run review')}"
                f"<p style='margin-top:10px'><a class=btn href='{link('archive', pr)}'>Archive"
                f"</a></p></div>"), user=user, active="queue"))

        if st == "reviewing":
            s = (STATE / pr / "status").read_text().strip().lower()
            phases = ["Fetching the PR", "Checking out the branch",
                      "Reviewing the diff", "Writing the findings"]
            cur = (0 if "fetch" in s else 1 if ("checking out" in s or "queued" in s)
                   else 2 if "reviewing" in s else 3)
            steps = []
            for j, ph in enumerate(phases):
                if j < cur:
                    steps.append(f"<li class=done><span class=pm>✓</span>{ph}</li>")
                elif j == cur:
                    steps.append(f"<li class=now><span class='pm spin'></span>{ph}</li>")
                else:
                    steps.append(f"<li><span class=pm>○</span>{ph}</li>")
            queued = ("<div class=hint>Waiting for another review to finish first — one runs at "
                      "a time on this box.</div>" if "queued" in s else "")
            reff = review_effort(pr) or "standard"
            panel = (
                "<div class=card><div class=prog-hd>Drafting review for "
                f"<b>#{pr}</b> · <span class='muted sm'>{EFFORT[reff][0]} effort</span></div>"
                f"<ul class=prog>{''.join(steps)}</ul>"
                "<div class=progbar><div class=progfill></div></div>"
                f"{queued}<div class='hint' style='margin-top:10px'>This page refreshes itself; "
                f"{EFFORT[reff][2]}.</div></div>")
            return self.reply(200, shell(f"#{pr}", head + panel, refresh=link("pr", pr),
                                         user=user, active="queue"))

        rev = load_review(pr)
        if not rev:
            note = ""
            if st == "failed":
                s = (STATE / pr / "status").read_text().strip()
                note = (f"<div class='banner err'><span>🔴</span><div>{html.escape(s)}</div>"
                        f"</div>")
            approved = (self.approved_card(pr, user)
                        if marker(pr, "approved", user).get("at") else "")
            body = (f"<div class=card><h4>Not reviewed here</h4>"
                    f"<p class='muted sm'>No review has been run for this PR on this box.</p>"
                    f"{self.effort_picker(pr, meta)}</div>") if not approved else approved
            return self.reply(200, shell(f"#{pr}",
                                         head + note + body + self.footer_actions(pr, user),
                                         user=user, active="queue"))

        return self.reply(200, shell(f"#{pr}", head + self.review_body(pr, rev, user),
                                     user=user, active="queue"))

    def review_body(self, pr, rev, user):
        ev = rev.get("event", "COMMENT")
        comments = sorted(rev.get("comments", []),
                          key=lambda c: SEV_ORDER.get(c.get("severity"), 9))
        cs = sev_counts(comments)
        blockers = cs.get("blocker", 0)
        parts = [dry_banner()]

        chips = "".join(pill(s, f"{n} {SEV_LABEL.get(s, s)}")
                        for s, n in sorted(cs.items(), key=lambda kv: SEV_ORDER.get(kv[0], 9)))
        verdict_pill = pill("blocker" if ev == "REQUEST_CHANGES" else "posted", ev)
        parts.append(f"<h2>Assessment</h2><div class=card><div class=meta>{verdict_pill}"
                     f"<span class='muted sm'>the agent's read — comments post as a plain "
                     f"review either way</span></div>"
                     f"{prbot_md.render(rev.get('summary', ''))}"
                     + (f"<div class=chips>{chips}</div>" if chips else "") + "</div>")

        # Collapsed by default: the findings are the working surface, the prose is reference.
        if rev.get("explainer"):
            parts.append(f"<details><summary>What this PR does</summary>"
                         f"<div class=dbody>{prbot_md.render(rev['explainer'])}</div></details>")
        if rev.get("analysis"):
            parts.append(f"<details><summary>Analysis — what I checked, and what I dropped"
                         f"</summary><div class=dbody>{prbot_md.render(rev['analysis'])}"
                         f"</div></details>")

        exp_p, sig_p = mint("post", pr, ACTION_TTL)
        exp_a, sig_a = mint("approve", pr, ACTION_TTL)

        parts.append(f"<h2>Findings ({len(comments)})</h2>")
        if not comments:
            parts.append("<div class=card><p class=muted>No findings — nothing to post.</p>"
                         "</div>")
        else:
            parts.append("<p class=sm><a href='javascript:all(true)'>select all</a> · "
                         "<a href='javascript:all(false)'>select none</a></p>")

        def finding_card(i, c, checked):
            sev = c.get("severity", "nit")
            loc = f"{c.get('path', '?')}:{c.get('line', '?')}"
            thread = (f"↩ reply to {html.escape(c['reply_to'])}" if c.get("reply_to")
                      else "new thread")
            sugg = c.get("suggestion", "")
            sugg_box = ""
            if sugg:
                sugg_box = (
                    "<div class=sugg><div class=sugglabel>💡 Suggested change — the author can "
                    "apply this in one click on GitHub</div>"
                    f"<textarea class=suggin name='sugg_{i}'>{html.escape(sugg)}</textarea></div>")
            return (
                f"<div class=finding><div class=fhead>"
                f"<input type=checkbox class=fsel name='sel_{i}' id='sel_{i}'{' checked' if checked else ''}>"
                f"<label for='sel_{i}'>{pill(sev, SEV_LABEL.get(sev, sev))}</label>"
                f"<span class=loc>{html.escape(loc)}</span>"
                f"<span class=thread>{thread}</span></div>"
                f"<div class=fbody>"
                f"<textarea name='body_{i}'>{html.escape(c.get('body', ''))}</textarea>"
                + sugg_box
                + f"<input type=hidden name='path_{i}' value='{html.escape(c.get('path', ''))}'>"
                f"<input type=hidden name='line_{i}' value='{c.get('line', '')}'>"
                f"<input type=hidden name='sev_{i}' value='{html.escape(sev)}'>"
                f"</div></div>")

        # Low-confidence findings go into a collapsed "Maybe" tray, unchecked, so they add no
        # noise but stay one click away. Everything else is checked by default.
        fields, maybe = [], []
        for i, c in enumerate(comments):
            low = c.get("confidence") == "low"
            (maybe if low else fields).append(finding_card(i, c, checked=not low))
        maybe_tray = ""
        if maybe:
            maybe_tray = (
                f"<details class=maybe><summary>🤔 Maybe — {len(maybe)} lower-confidence "
                "finding" + ("s" if len(maybe) != 1 else "") + " (unchecked)</summary>"
                "<div class=dbody>" + "".join(maybe) + "</div></details>")

        posted = upath(pr, user, "posted.json").exists()
        posted_note = ("<div class='banner ok'><span>✓</span><div>You already posted this to "
                       "GitHub. Posting again adds a second review.</div></div>"
                       if posted else "")
        label = "Post selected" + (" (dry run)" if DRY_RUN else " to GitHub")
        parts.append(
            f"<form method=post action='/prbot/post'>{posted_note}"
            + "".join(fields) + maybe_tray
            + f"<input type=hidden name=pr value='{pr}'>"
              f"<input type=hidden name=exp value='{exp_p}'>"
              f"<input type=hidden name=sig value='{sig_p}'>"
              f"<input type=hidden name=count value='{len(comments)}'>"
              f"<div class=bar><div class=inner>"
              f"<span class='muted sm'><b id=cnt>0</b> selected · posts as plain comments, "
              f"does not request changes</span>"
              f"<span class=spacer></span>"
              f"{self.effort_rerun(pr)}"
              f"<button class='btn primary' id=submit type=submit>{label}</button>"
              f"</div></div></form>")

        # --- approve -----------------------------------------------------------------
        if marker(pr, "approved", user).get("at"):
            parts.append(self.approved_card(pr, user))
            parts.append(self.footer_actions(pr, user))
            return "".join(parts)

        lgtm = blockers == 0 and ev != "REQUEST_CHANGES"
        default_msg = default_approve_msg(rev)
        if lgtm:
            vb = ("<div class='banner ok'><span>✅</span><div><b>LGTM</b> — no blockers.</div>"
                  "</div>")
        else:
            why = (f"{blockers} blocker(s)" if blockers
                   else "the agent's assessment is <code>REQUEST_CHANGES</code>")
            vb = (f"<div class='banner warn'><span>⚠️</span><div><b>Not LGTM</b> — {why}. "
                  f"Approving anyway needs the confirmation below.</div></div>")
        ack = "" if lgtm else (
            "<p class=sm><label><input type=checkbox name=ack required> I've read the "
            "findings above and want to approve anyway.</label></p>")
        parts.append(
            f"<h2>Approve</h2><div class=card>{vb}"
            f"<form method=post action='/prbot/approve'>"
            f"<label class='muted sm'>Approval comment — posted on the PR as a whole, "
            f"then the PR is approved</label>"
            f"<textarea class=short name=approve_body>{html.escape(default_msg)}</textarea>"
            f"{ack}"
            f"<input type=hidden name=pr value='{pr}'>"
            f"<input type=hidden name=exp value='{exp_a}'>"
            f"<input type=hidden name=sig value='{sig_a}'>"
            f"<p><button class='btn warn' type=submit>"
            + ("Approve (dry run)" if DRY_RUN else f"Approve #{pr} as {html.escape(user)}")
            + "</button></p></form>"
              "<p class='muted sm'>Checks the PR is open, is not yours, and was reviewed "
              "here. This token lasts 30 minutes — reload if it lapses.</p>"
              "</div>")
        parts.append(self.footer_actions(pr, user))
        return "".join(parts)

    def approved_card(self, pr, user):
        appr = marker(pr, "approved", user)
        said = appr.get("body", "")
        manual = appr.get("manual")
        return (f"<h2>Approved</h2><div class=card>"
                f"<div class='banner ok'><span>✅</span><div><b>"
                + ("Marked as approved" if manual else "Approved")
                + f" on {fmt_date(appr['at'])}</b> ({ago(appr['at'])})"
                + ("" if manual else f" as <code>{html.escape(user)}</code>")
                + ("<br>Recorded here only — the approval itself was done on GitHub."
                   if manual else "") + "</div></div>"
                + (f"<p class='muted sm'>Comment posted with the approval:</p>"
                   f"<pre><code>{html.escape(said)}</code></pre>"
                   if said and not manual else "")
                + f"<p><a class=btn href='{ghurl_of(pr)}'>View on GitHub</a></p></div>")

    def footer_actions(self, pr, user):
        """Local-only housekeeping: never touches GitHub."""
        exp_m, sig_m = mint("markdone", pr, ACTION_TTL)
        st = pr_state(pr, user)
        marked = ""
        if st != "approved":
            marked = (
                f"<form method=post action='/prbot/markdone' style='display:inline'>"
                f"<input type=hidden name=pr value='{pr}'>"
                f"<input type=hidden name=exp value='{exp_m}'>"
                f"<input type=hidden name=sig value='{sig_m}'>"
                f"<button class=btn type=submit>Mark as approved</button></form> ")
        return (f"<h2>Housekeeping</h2><div class=card>"
                f"<p class='muted sm'>Local to this dashboard — neither button touches "
                f"GitHub.</p><p>{marked}"
                f"<a class=btn href='{link('archive', pr)}'>Archive</a></p></div>")

    # -- actions -----------------------------------------------------------------------------
    def effort_picker(self, pr, meta, label="Run review"):
        """Three effort buttons; the size-suggested one highlighted. This is where a reviewer
        says 'review this shallow / deep' per PR."""
        suggested = autosize_effort(meta)
        base = link("review", pr)
        btns = []
        for k in EFFORT_ORDER:
            name, sub, _ = EFFORT[k]
            hot = (k == suggested)
            cls = "btn primary" if hot else "btn soft"
            btns.append(
                f"<a class='{cls} eff' href='{base}&effort={k}'>"
                f"<span class=effname>{name}{' · suggested' if hot else ''}</span>"
                f"<span class=effsub>{sub}</span></a>")
        return (f"<div class=effort><div class=effort-lbl>{label} at</div>"
                f"<div class=effrow>{''.join(btns)}</div>"
                "<div class=hint>Suggested from the PR size — you choose. Deeper reviews cost "
                "more of your weekly Claude usage.</div></div>")

    def effort_rerun(self, pr):
        """Compact 'Re-run: Quick · Standard · Deep' for the sticky bar on a finished review."""
        base = link("review", pr)
        links = " · ".join(f"<a href='{base}&effort={k}'>{EFFORT[k][0]}</a>"
                           for k in EFFORT_ORDER)
        return f"<span class='muted sm rerun'>Re-run: {links}</span>"

    def start_review(self, pr, user, force=False, effort=""):
        d = STATE / pr
        d.mkdir(parents=True, exist_ok=True)
        touch_user(pr, user)
        # run-review.sh takes a per-PR flock, so a genuine duplicate is impossible — only skip
        # when a review is ACTUALLY running. This lets a finished review be re-run and, crucially,
        # a stalled one (status stuck at "reviewing" but the process is gone) be recovered; the
        # old "idle" guard read the stale status and refused, so Re-run appeared to do nothing.
        if not is_running(pr):
            meta, _ = pr_meta(pr)
            eff = effort if effort in EFFORT else autosize_effort(meta)
            (d / "effort").write_text(eff)
            (d / "status").write_text("queued")
            env = review_env(user)
            env["PRBOT_EFFORT"] = eff
            with open(d / "run.log", "ab") as log:
                subprocess.Popen([str(BIN / "run-review.sh"), pr], stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True, env=env)
        return self.redirect(link("pr", pr))

    def do_post_comments(self, pr, user, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        rev = load_review(pr) or {}
        chosen = []
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
            chosen.append({"path": one(f"path_{i}"),
                           "line": int(line) if line.isdigit() else None,
                           "severity": one(f"sev_{i}"),
                           "body": body})
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
            return self.detail_page(pr, user, "<div class='banner warn'><span>⚠️</span><div>"
                                              "Nothing selected — nothing sent.</div></div>")

        # Re-validate anchors against the CURRENT diff: the PR may have gained commits while
        # this review sat in the dashboard, and one stale line 422s the whole review.
        files, err = fetch_pr_files(pr)
        if err is not None:
            return self.detail_page(pr, user, (
                f"<div class='banner err'><span>🔴</span><div><b>Could not fetch the PR diff "
                f"from GitHub — nothing was posted.</b><br>Without it every comment would be "
                f"demoted out of the diff and posted as a plain summary, so this refuses "
                f"rather than posting a degraded review. Check "
                f"<a href='https://www.githubstatus.com'>githubstatus.com</a> and retry.<br>"
                f"<code>{html.escape(err)}</code></div></div>"))
        inline, orphans = prbot_diff.split_anchorable(chosen, prbot_diff.anchor_map(files))
        # No bot signature: this posts under the reviewer's own account, so GitHub already
        # attributes it. A trailing "Reviewed by @x" only restates the byline.
        body = (rev.get("summary") or "").strip() + prbot_diff.orphan_block(orphans)
        # Always a plain comment review. REQUEST_CHANGES blocks the PR and reads as a
        # verdict; these are review notes, and the human approves separately.
        event = "COMMENT"
        payload = {"body": body, "event": event, "comments": inline}
        ud = udir(pr, user)
        ud.mkdir(parents=True, exist_ok=True)
        (ud / "payload.json").write_text(json.dumps(payload))

        if DRY_RUN:
            return self.detail_page(pr, user, (
                f"<div class='banner warn'><span>🧪</span><div><b>DRY RUN — nothing was sent "
                f"to GitHub.</b><br>Would post {len(inline)} inline comment(s)"
                + (f", {len(orphans)} folded into the summary" if orphans else "")
                + f", as <code>{event}</code>. Set <code>DRY_RUN=0</code> and restart "
                  f"<code>prbot</code> to post for real.</div></div>"))

        tok = user_pat(user)
        if not tok:
            return self.detail_page(pr, user, "<div class='banner err'><span>🚫</span><div>"
                                              "Your stored GitHub token could not be read — "
                                              "paste it again in <a href='/prbot/settings'>"
                                              "settings</a>.</div></div>")
        r = gh(["api", "--method", "POST", f"repos/{REPO}/pulls/{pr}/reviews",
                "--input", str(ud / "payload.json")], token=tok)
        if r.returncode != 0:
            return self.detail_page(pr, user, f"<div class='banner err'><span>🔴</span><div>"
                                              f"GitHub rejected it: <code>"
                                              f"{html.escape(r.stderr[:400])}</code></div></div>")
        (ud / "posted.json").write_text(
            json.dumps({"at": int(time.time()), "inline": len(inline), "event": event}))
        return self.detail_page(pr, user, (
            f"<div class='banner ok'><span>✓</span><div>Posted {len(inline)} comment(s) as "
            f"<code>{event}</code> under <code>{html.escape(user)}</code>."
            + (f" {len(orphans)} could not be anchored and went into the summary."
               if orphans else "") + "</div></div>"))

    def do_approve(self, pr, user, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        rev = load_review(pr) or {}
        blockers = sev_counts(rev.get("comments", [])).get("blocker", 0)
        lgtm = blockers == 0 and rev.get("event") != "REQUEST_CHANGES"
        if not lgtm and not one("ack"):
            return self.detail_page(pr, user, "<div class='banner warn'><span>⚠️</span><div>"
                                              "This review is not LGTM — tick the "
                                              "confirmation to approve anyway.</div></div>")
        msg = one("approve_body").strip() or "LGTM."
        tok = user_pat(user)
        if not tok:
            return self.detail_page(pr, user, "<div class='banner err'><span>🚫</span><div>"
                                              "Your stored GitHub token could not be read — "
                                              "paste it again in <a href='/prbot/settings'>"
                                              "settings</a>.</div></div>")
        ok, why = can_approve(pr, user)
        if not ok:
            return self.detail_page(pr, user, f"<div class='banner err'><span>🚫</span><div>"
                                              f"{html.escape(why)}</div></div>")
        if DRY_RUN:
            return self.detail_page(pr, user, (
                f"<div class='banner warn'><span>🧪</span><div><b>DRY RUN — not approved.</b>"
                f"<br>Would submit an APPROVE review as <code>{html.escape(user)}</code> with "
                f"body: <em>{html.escape(msg[:200])}</em></div></div>"))
        r = gh(["api", "--method", "POST", f"repos/{REPO}/pulls/{pr}/reviews",
                "-f", "event=APPROVE", "-f", f"body={msg}"], token=tok)
        if r.returncode != 0:
            return self.detail_page(pr, user, f"<div class='banner err'><span>🔴</span><div>"
                                              f"GitHub rejected it: <code>"
                                              f"{html.escape(r.stderr[:400])}</code></div></div>")
        ud = udir(pr, user)
        ud.mkdir(parents=True, exist_ok=True)
        (ud / "approved").write_text(json.dumps({"at": int(time.time()), "body": msg}))
        return self.detail_page(pr, user, f"<div class='banner ok'><span>✅</span><div>Approved "
                                          f"#{pr} as <code>{html.escape(user)}</code>."
                                          f"</div></div>")


if __name__ == "__main__":
    port = int(os.environ.get("PRBOT_PORT", "8899"))
    if USERS.exists():
        os.chmod(USERS, 0o600)
    print(f"prbot listening on 127.0.0.1:{port} (dry_run={DRY_RUN}, "
          f"users={len(load_users())})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
