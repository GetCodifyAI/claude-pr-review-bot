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
import calendar
import fcntl
import hmac
import html
import json
import os
import re
import subprocess
import time
from hashlib import sha256
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

import prbot_diff
import prbot_md

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
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--line2:#21262d;--fg:#e6edf3;
--dim:#9198a1;--accent:#4493f8}
body{font:15px/1.7 -apple-system,system-ui,"Segoe UI",sans-serif;background:var(--bg);
color:var(--fg);margin:0;padding:0 0 140px}
.wrap{max-width:64rem;margin:0 auto;padding:26px 20px 0}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:1.35rem;margin:0 0 6px;line-height:1.35}
h2{font-size:.82rem;margin:30px 0 12px;color:var(--dim);text-transform:uppercase;
letter-spacing:.07em;font-weight:700}
h4,h5,h6{font-size:.98rem;margin:20px 0 8px;color:var(--fg)}
p{margin:10px 0}
.muted{color:var(--dim)}.sm{font-size:.87rem}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.crumb{font-size:.87rem;margin:0 0 14px}
.meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:.87rem;
color:var(--dim);margin:0 0 4px}
code{background:#0d1117;border:1px solid var(--line2);padding:1px 5px;border-radius:5px;
font-size:.86em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:#010409;border:1px solid var(--line2);border-radius:9px;padding:13px 15px;
overflow-x:auto;margin:12px 0}pre code{border:0;padding:0;background:none;font-size:.83rem}
blockquote{border-left:3px solid var(--line);margin:12px 0;padding:2px 0 2px 14px;
color:var(--dim)}
ul,ol{margin:10px 0;padding-left:22px}li{margin:5px 0}
.tw{overflow-x:auto;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:.89rem}
th,td{text-align:left;padding:8px 11px;border-bottom:1px solid var(--line2);
vertical-align:top}
th{color:var(--dim);font-size:.76rem;text-transform:uppercase;letter-spacing:.05em}
tbody tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.72rem;
font-weight:700;letter-spacing:.03em;text-transform:uppercase;white-space:nowrap}
.blocker{background:#3d1014;color:#ff8087}.should-fix{background:#452709;color:#ffbe6a}
.nit{background:#16304f;color:#84b6f4}.question{background:#2a1f42;color:#c8a6f7}
.new{background:#122b1c;color:#79d18a}.reviewing{background:#452709;color:#ffbe6a}
.done{background:#16304f;color:#84b6f4}.posted{background:#122b1c;color:#79d18a}
.approved{background:#122b1c;color:#79d18a}.failed{background:#3d1014;color:#ff8087}
.dry{background:#452709;color:#ffbe6a}.stalled{background:#3d1014;color:#ff8087}
.archived{background:#21262d;color:#8b949e}
details{border:1px solid var(--line);border-radius:12px;background:var(--panel);margin:12px 0}
details[open]{padding-bottom:6px}
summary{cursor:pointer;padding:14px 20px;font-weight:600;font-size:.95rem;list-style:none;
display:flex;justify-content:space-between;align-items:center;gap:10px}
summary::-webkit-details-marker{display:none}
summary::after{content:"▾";color:var(--dim);font-size:.85rem;transition:transform .15s}
details[open]>summary::after{transform:rotate(180deg)}
summary:hover{background:#1c2129;border-radius:12px}
.dbody{padding:0 20px 6px;border-top:1px solid var(--line2);margin-top:2px}
.finding{border:1px solid var(--line);border-radius:12px;padding:0;margin:12px 0;
background:var(--panel);overflow:hidden;transition:opacity .15s}
.finding.off{opacity:.4}
.fhead{display:flex;gap:11px;align-items:center;flex-wrap:wrap;padding:13px 18px;
background:#1a1f27;border-bottom:1px solid var(--line2)}
.loc{font-family:ui-monospace,Menlo,monospace;font-size:.83rem;color:#c9d1d9;
word-break:break-all}
.thread{font-size:.76rem;color:var(--dim);margin-left:auto;white-space:nowrap}
.fbody{padding:14px 18px}
textarea{width:100%;background:#010409;color:var(--fg);border:1px solid var(--line);
border-radius:9px;padding:12px 14px;font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
resize:vertical;min-height:150px}
textarea:focus{outline:0;border-color:var(--accent)}
textarea.short{min-height:76px}
input[type=checkbox]{width:17px;height:17px;accent-color:var(--accent);cursor:pointer;
flex:none}
label{cursor:pointer}
select{background:#0d1117;color:var(--fg);border:1px solid var(--line);border-radius:8px;
padding:8px 11px;font-size:.88rem}
.btn{display:inline-block;background:#21262d;color:var(--fg);border:1px solid var(--line);
padding:9px 16px;border-radius:9px;font-size:.9rem;cursor:pointer;font-weight:600;
font-family:inherit}
.btn:hover{background:#2b3138}
.btn.primary{background:#238636;border-color:#2ea043;color:#fff}
.btn.primary:hover{background:#2ea043}
.btn.warn{background:#8b2c1e;border-color:#a33b2a;color:#fff}.btn.warn:hover{background:#a33b2a}
.btn.ghost{background:transparent}
.btn:disabled{opacity:.5;cursor:not-allowed}
.bar{position:fixed;left:0;right:0;bottom:0;background:rgba(13,17,23,.96);
border-top:1px solid var(--line);backdrop-filter:blur(8px);z-index:20}
.bar .inner{max-width:64rem;margin:0 auto;padding:12px 20px;display:flex;gap:12px;
align-items:center;flex-wrap:wrap}
.spacer{flex:1}
.banner{border-radius:10px;padding:13px 16px;margin:14px 0;font-size:.9rem;
display:flex;gap:11px;align-items:flex-start}
.banner.warn{background:#33240a;border:1px solid #6b4a08;color:#ffd07b}
.banner.ok{background:#0f2419;border:1px solid #1f5132;color:#7ee2a0}
.banner.err{background:#341215;border:1px solid #6e2a2f;color:#ff9aa2}
.banner b{color:#fff}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0 0}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin:18px 0 0;border-bottom:1px solid var(--line);
padding-bottom:0}
.tab{padding:9px 14px;border-radius:9px 9px 0 0;font-size:.88rem;color:var(--dim);
border:1px solid transparent;border-bottom:0;display:flex;gap:7px;align-items:center}
.tab:hover{color:var(--fg);text-decoration:none;background:#161b22}
.tab.on{color:var(--fg);background:var(--panel);border-color:var(--line);font-weight:600}
.cnt{background:#21262d;border-radius:999px;padding:0 7px;font-size:.72rem;color:var(--dim)}
.tab.on .cnt{background:#2b3138;color:var(--fg)}
.sortbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:14px 0 10px}
.sortopt{font-size:.82rem;color:var(--dim);padding:4px 10px;border-radius:999px;
border:1px solid transparent}
.sortopt:hover{color:var(--fg);text-decoration:none;background:#161b22}
.sortopt.on{color:var(--fg);background:#21262d;border-color:var(--line)}
.rowsub{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.rowsub>span:not(.chipwrap):not(:last-child)::after{content:"·";margin-left:6px;
color:var(--line)}
.chipwrap{display:inline-flex;gap:5px;flex-wrap:wrap}
.timeline{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0 0;
font-size:.84rem;color:var(--dim)}
.step{display:flex;gap:6px;align-items:center;background:#0d1117;border:1px solid var(--line2);
border-radius:999px;padding:4px 12px}
.step.hit{border-color:#1f5132;color:#7ee2a0}
.row{display:flex;align-items:stretch;border-bottom:1px solid var(--line2)}
.row:last-child{border-bottom:0}
.row:hover{background:#1a1f27}
.rowact{align-self:center;flex:none;font-size:.78rem;color:var(--dim);padding:6px 14px;
margin-right:8px;border:1px solid var(--line2);border-radius:8px;white-space:nowrap}
.rowact:hover{color:var(--fg);background:#21262d;text-decoration:none}
.rowlink{display:block;flex:1;min-width:0;padding:13px 16px}
.rowlink:hover{text-decoration:none}
.rowtop{display:flex;gap:10px;align-items:center;margin-bottom:3px}
.num{font-family:ui-monospace,Menlo,monospace;color:var(--accent);font-weight:600}
.ttl{color:var(--fg);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.list{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
@media(max-width:640px){.wrap{padding:18px 14px 0}.thread{margin-left:0}
.bar .inner{padding:10px 14px}h1{font-size:1.15rem}}
"""

JS = """
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
"""


def shell(title, body, refresh=None):
    r = f'<meta http-equiv=refresh content="8;url={refresh}">' if refresh else ""
    return (f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>{r}<style>{CSS}</style>"
            f"<div class=wrap>{body}</div><script>{JS}</script>")


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
        if route == "/settings":
            return self.settings_page(user)
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
            return self.start_review(pr, user, force=True)
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
        if route == "/settings":
            return self.do_settings(user, form)
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
            return self.login_page(nxt, (
                f"<div class='banner err'><span>🚫</span><div><b>GitHub signed you in, but the "
                f"token cannot see <code>{html.escape(REPO)}</code>.</b><br>An org owner needs "
                f"to allow this app once: for an OAuth App, approve it under the org's "
                f"<em>Third-party access</em> settings; for a GitHub App, install it on the org. "
                f"Until then, sign in with a token below.</div></div>"))
        prev = load_users().get(login) or {}
        oauth_store(login, d, name, prev)
        print(f"login (github): {login}", flush=True)
        # First sign-in with no Slack ID: land on settings so the ping actually reaches them.
        if not prev.get("slack_id"):
            nxt = "/prbot/settings"
        return self.redirect(nxt, cookie=session_cookie(login))

    def login_page(self, nxt="/prbot/", banner=""):
        if not nxt.startswith("/prbot/"):
            nxt = "/prbot/"
        github_btn = ""
        if OAUTH_ENABLED:
            github_btn = (
                "<div class=card style='margin-bottom:14px'>"
                "<h4 style='margin-top:0'>Sign in with GitHub</h4>"
                "<p class='muted sm'>One click. GitHub issues this dashboard a short-lived "
                "token that acts as you — comments and approvals carry your name. Nothing to "
                "paste, nothing to rotate.</p>"
                f"<p><a class='btn primary' href='/prbot/oauth/start?next={quote(nxt, safe='')}'>"
                "Sign in with GitHub</a></p></div>"
                "<details><summary>Or sign in with a token instead</summary><div class=dbody>")
        body = (
            "<h1>PR review bot</h1>"
            f"<p class='muted sm'>Reviewing <code>{html.escape(REPO)}</code></p>" + banner
            + github_btn
            + "<div class=card><form method=post action='/prbot/login'>"
              "<h4 style='margin-top:0'>Sign in with your GitHub token</h4>"
              "<p class='muted sm'>A <b>classic</b> personal access token with the "
              "<code>repo</code> scope — GitHub → Settings → Developer settings → Tokens "
              "(classic). It is used for exactly one thing: posting the comments and "
              "approvals <em>you</em> choose, under <em>your</em> name. Stored encrypted on "
              "this box; never shown again.</p>"
              "<label class='muted sm'>GitHub token</label>"
              "<textarea class=short name=pat placeholder='ghp_…' required "
              "style='min-height:52px'></textarea>"
              "<label class='muted sm'>Slack member ID <span class=muted>(optional — so "
              "review requests ping you)</span></label>"
              "<textarea class=short name=slack_id placeholder='U0123ABCDEF' "
              "style='min-height:52px'></textarea>"
              "<p class='muted sm'>Find it in Slack: your profile → ⋮ → <b>Copy member ID</b>."
              "</p>"
              f"<input type=hidden name=next value='{html.escape(nxt)}'>"
              "<p><button class='btn primary' type=submit>Sign in</button></p>"
              "</form></div>"
            + ("</div></details>" if OAUTH_ENABLED else "")
            + "<p class='muted sm'>Reviews themselves run on the shared team runner — there "
              "is nothing to connect for that.</p>")
        return self.reply(200, shell("Sign in", body))

    def do_login(self, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        pat, slack_id, nxt = one("pat").strip(), one("slack_id").strip(), one("next")
        if not pat:
            return self.login_page(nxt, "<div class='banner warn'><span>⚠️</span><div>Paste "
                                        "a token.</div></div>")
        login, name, err = verify_pat(pat)
        if err:
            return self.login_page(nxt, f"<div class='banner err'><span>🚫</span><div>"
                                        f"{html.escape(err)}</div></div>")
        users = load_users()
        prev = users.get(login) or {}
        users[login] = {"pat_enc": enc(pat), "name": name,
                        "slack_id": slack_id or prev.get("slack_id", ""),
                        "added": prev.get("added") or int(time.time()),
                        "updated": int(time.time())}
        save_users(users)
        print(f"login: {login}", flush=True)
        if not nxt.startswith("/prbot/"):
            nxt = "/prbot/"
        return self.redirect(nxt, cookie=session_cookie(login))

    def settings_page(self, user, banner=""):
        u = load_users().get(user) or {}
        exp, sig = mint("settings", user, ACTION_TTL)
        body = (
            f"<p class=crumb><a href='/prbot/'>← queue</a></p><h1>Settings</h1>"
            f"<p class='muted sm'>Signed in as <code>{html.escape(user)}</code></p>" + banner
            + "<div class=card><form method=post action='/prbot/settings'>"
              "<h4 style='margin-top:0'>Slack</h4>"
              "<p class='muted sm'>Cards for PRs awaiting your review will @-mention this "
              "member ID. Your profile → ⋮ → <b>Copy member ID</b>.</p>"
              f"<textarea class=short name=slack_id style='min-height:52px'>"
              f"{html.escape(u.get('slack_id', ''))}</textarea>"
              "<h4>GitHub</h4>"
            + ("<p class='muted sm'>Signed in with GitHub — your token is issued by GitHub "
               "and refreshed automatically. You can still paste a personal token below to "
               "use that instead.</p>" if u.get("gh_token_enc") else
               "<p class='muted sm'>Leave blank to keep the current token. Paste a new one "
               "to replace it.</p>")
            + 
              "<textarea class=short name=pat placeholder='ghp_… (optional)' "
              "style='min-height:52px'></textarea>"
              "<h4>Claude</h4>"
              "<p class='muted sm'>Reviews run on the shared team runner on this box. "
              "Nothing to connect.</p>"
              f"<input type=hidden name=exp value='{exp}'>"
              f"<input type=hidden name=sig value='{sig}'>"
              "<p><button class='btn primary' type=submit>Save</button> "
              "<a class=btn href='/prbot/logout'>Sign out</a></p>"
              "</form></div>")
        return self.reply(200, shell("Settings", body))

    def do_settings(self, user, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        if err := verify("settings", user, one("exp"), one("sig")):
            return self.deny(err)
        users = load_users()
        u = users.get(user) or {}
        u["slack_id"] = one("slack_id").strip()
        pat = one("pat").strip()
        if pat:
            login, name, err = verify_pat(pat)
            if err:
                return self.settings_page(user, f"<div class='banner err'><span>🚫</span>"
                                                f"<div>{html.escape(err)}</div></div>")
            if login != user:
                return self.settings_page(user, f"<div class='banner err'><span>🚫</span>"
                                                f"<div>That token belongs to <code>"
                                                f"{html.escape(login)}</code>, not you.</div>"
                                                f"</div>")
            u["pat_enc"], u["name"] = enc(pat), name
        u["updated"] = int(time.time())
        users[user] = u
        save_users(users)
        return self.settings_page(user, "<div class='banner ok'><span>✓</span><div>Saved."
                                        "</div></div>")

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
            rows.append(
                f"<div class=row><a class=rowlink href='{link('pr', num)}'>"
                f"<div class=rowtop><span class=num>#{num}</span>"
                f"<span class=ttl>{html.escape(item.get('title', ''))}</span>"
                f"{pill(e['st'])}</div>"
                f"<div class='muted sm rowsub'>"
                f"<span>{html.escape(item.get('author', ''))}</span>"
                + (f"<span>{size}</span>" if size else "")
                + "".join(f"<span>{html.escape(w)}</span>" for w in when)
                + (f"<span class=chipwrap>{chips}</span>" if chips else "")
                + "</div></a>"
                + (f"<a class=rowact href='{link('unarchive', num)}' "
                   f"title='Move back to the queue'>restore</a>"
                   if e["st"] == "archived" else
                   f"<a class=rowact href='{link('archive', num)}' "
                   f"title='Hide from the queue'>archive</a>") + "</div>")

        empty = {"todo": "Nothing waiting on you.",
                 "reviewed": "No reviews waiting to be posted.",
                 "posted": "Nothing posted and unapproved.",
                 "approved": "Nothing approved yet."}.get(tab, "Queue is empty.")
        slack_ok = bool((load_users().get(user) or {}).get("slack_id"))
        body = (f"<h1>PR review queue</h1>"
                f"<p class='muted sm'>Reviewing as <code>{html.escape(user)}</code>"
                + ("" if DRY_RUN else " · <b>live</b> — posting and approving write to GitHub")
                + " · <a href='/prbot/settings'>settings</a>"
                  " · <a href='/prbot/logout'>sign out</a>"
                + f"</p>{dry_banner()}"
                + ("" if slack_ok else
                   "<div class='banner warn'><span>💬</span><div>No Slack member ID yet — "
                   "review requests will not ping you. <a href='/prbot/settings'>Add it in "
                   "settings.</a></div></div>")
                + f"<div class=tabs>{tabs}</div>"
                  f"<div class=sortbar><span class='muted sm'>Sort</span>{sorts}</div>"
                  f"<div class=list>"
                + ("".join(rows) or f"<div class='rowlink muted'>{empty}</div>")
                + "</div>")
        return self.reply(200, shell("PR review queue", body))

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
        head = (f"<p class=crumb><a href='{link('', '')}'>← queue</a></p>"
                f"<h1>#{pr} — {html.escape(title)}</h1>"
                f"<div class=meta>{pill(st)}"
                + (pill("dry", "dry run") if DRY_RUN else "")
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
                f"<div class=card><p><a class='btn primary' href='{link('review', pr)}'>"
                f"Re-run review</a> <a class=btn href='{link('archive', pr)}'>Archive</a></p>"
                f"</div>")))

        if st == "reviewing":
            s = (STATE / pr / "status").read_text().strip()
            return self.reply(200, shell(f"#{pr}", head + (
                f"<div class=card><h4>Working…</h4><p class='muted sm'>Status: <code>"
                f"{html.escape(s)}</code>. This page refreshes itself; a 25-file PR takes "
                f"10–15 minutes.</p></div>"), refresh=link("pr", pr)))

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
                    f"<p><a class='btn primary' href='{link('review', pr)}'>Run review</a></p>"
                    f"</div>") if not approved else approved
            return self.reply(200, shell(f"#{pr}",
                                         head + note + body + self.footer_actions(pr, user)))

        return self.reply(200, shell(f"#{pr}", head + self.review_body(pr, rev, user)))

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

        fields = []
        for i, c in enumerate(comments):
            sev = c.get("severity", "nit")
            loc = f"{c.get('path', '?')}:{c.get('line', '?')}"
            thread = (f"↩ reply to {html.escape(c['reply_to'])}" if c.get("reply_to")
                      else "new thread")
            fields.append(
                f"<div class=finding><div class=fhead>"
                f"<input type=checkbox class=fsel name='sel_{i}' id='sel_{i}' checked>"
                f"<label for='sel_{i}'>{pill(sev, SEV_LABEL.get(sev, sev))}</label>"
                f"<span class=loc>{html.escape(loc)}</span>"
                f"<span class=thread>{thread}</span></div>"
                f"<div class=fbody>"
                f"<textarea name='body_{i}'>{html.escape(c.get('body', ''))}</textarea>"
                f"<input type=hidden name='path_{i}' value='{html.escape(c.get('path', ''))}'>"
                f"<input type=hidden name='line_{i}' value='{c.get('line', '')}'>"
                f"<input type=hidden name='sev_{i}' value='{html.escape(sev)}'>"
                f"</div></div>")

        posted = upath(pr, user, "posted.json").exists()
        posted_note = ("<div class='banner ok'><span>✓</span><div>You already posted this to "
                       "GitHub. Posting again adds a second review.</div></div>"
                       if posted else "")
        label = "Post selected" + (" (dry run)" if DRY_RUN else " to GitHub")
        parts.append(
            f"<form method=post action='/prbot/post'>{posted_note}"
            + "".join(fields)
            + f"<input type=hidden name=pr value='{pr}'>"
              f"<input type=hidden name=exp value='{exp_p}'>"
              f"<input type=hidden name=sig value='{sig_p}'>"
              f"<input type=hidden name=count value='{len(comments)}'>"
              f"<div class=bar><div class=inner>"
              f"<span class='muted sm'><b id=cnt>0</b> selected · posts as plain comments, "
              f"does not request changes</span>"
              f"<span class=spacer></span>"
              f"<a class='btn ghost' href='{link('review', pr)}'>Re-run</a>"
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
    def start_review(self, pr, user, force=False):
        d = STATE / pr
        d.mkdir(parents=True, exist_ok=True)
        touch_user(pr, user)
        cur = (d / "status").read_text().strip() if (d / "status").exists() else ""
        idle = not cur or cur.startswith(("done", "posted", "dry-run", "failed"))
        if idle and (force or not cur):
            (d / "status").write_text("queued")
            with open(d / "run.log", "ab") as log:
                subprocess.Popen([str(BIN / "run-review.sh"), pr], stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        return self.redirect(link("pr", pr))

    def do_post_comments(self, pr, user, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        rev = load_review(pr) or {}
        chosen = []
        for i in range(int(one("count") or 0)):
            if not form.get(f"sel_{i}"):
                continue
            line = one(f"line_{i}")
            chosen.append({"path": one(f"path_{i}"),
                           "line": int(line) if line.isdigit() else None,
                           "severity": one(f"sev_{i}"),
                           "body": one(f"body_{i}").strip()})
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
