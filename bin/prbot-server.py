#!/usr/bin/env python3
"""prbot-server.py — review dashboard, served on 127.0.0.1 behind Apache's /prbot proxy.

Reachable over the PUBLIC internet (the staging ALB answers *.staging.eng.cutanddry.com with
no auth in front), so every entry point is HMAC-signed. Page links last 7 days; the mutating
actions embedded in a page — posting comments, approving — carry their own 30-minute tokens
minted at render time, so a shared or bookmarked page URL cannot approve anything later.

Routes
  GET  /prbot/health
  GET  /prbot/?exp&sig                 index — every PR awaiting review, with state
  GET  /prbot/pr?pr=N&exp&sig          detail — full review, editable findings, actions
  GET  /prbot/review?pr=N&exp&sig      start a review, then redirect to the detail page
  POST /prbot/post                     post the selected (possibly edited) comments
  POST /prbot/approve                  approve as the reviewer
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
PAT = ENV.get("GITHUB_PAT", "")
REPO = ENV.get("REPO", "GetCodifyAI/cut-and-dry")
# No default: an inherited login would render someone else's queue and post under their
# name. bootstrap.sh always writes it, and lib-common.sh's require_env guards the CLI side.
REVIEWER = ENV.get("REVIEWER", "")
DRY_RUN = ENV.get("DRY_RUN", "1") == "1"


# --- signing ------------------------------------------------------------------------------
def sign(action, pr, exp):
    return hmac.new(SECRET.encode(), f"{action}:{pr}:{exp}".encode(), sha256).hexdigest()


def mint(action, pr, ttl):
    exp = int(time.time()) + ttl
    return exp, sign(action, pr, exp)


def link(action, pr, ttl=PAGE_TTL):
    exp, sig = mint(action, pr, ttl)
    q = f"?pr={pr}&exp={exp}&sig={sig}" if pr else f"?exp={exp}&sig={sig}"
    return f"/prbot/{action}{q}" if action else f"/prbot/{q}"


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
def gh(args, timeout=45):
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "GH_TOKEN": PAT})


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


def can_approve(pr):
    """(ok, why) — may REVIEWER approve this PR?

    Deliberately NOT "is REVIEWER a requested reviewer": GitHub clears the review request the
    moment any review is submitted, including a plain comment one. Gating on that made
    post-then-approve structurally impossible. What actually matters is that the PR is open,
    it is not REVIEWER's own PR (GitHub forbids self-approval), and this box genuinely
    reviewed it — which, combined with the per-PR HMAC on the link, is the real control.
    """
    r = gh(["api", f"repos/{REPO}/pulls/{pr}"])
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
    if ((d.get("user") or {}).get("login")) == REVIEWER:
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


def pr_state(pr):
    d = STATE / str(pr)
    if (d / "archived").exists():
        return "archived"
    if (d / "approved").exists():
        return "approved"
    if (d / "posted.json").exists():
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


def all_prs():
    """Queue order first, then anything reviewed earlier that has since left the queue."""
    live = queue()
    seen = {str(i.get("number")) for i in live}
    extra = []
    if STATE.exists():
        for d in STATE.iterdir():
            if d.is_dir() and d.name.isdigit() and d.name not in seen:
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


def marker(pr, name):
    """Read a state marker that may be plain-text (legacy) or JSON. Returns a dict."""
    f = STATE / str(pr) / name
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


def pr_times(pr):
    """Every timestamp we know about a PR, for sorting and display."""
    d = STATE / str(pr)
    rev_f = d / "review.json"
    return {
        "reviewed": int(rev_f.stat().st_mtime) if rev_f.exists() else 0,
        "posted": marker(pr, "posted.json").get("at", 0),
        "approved": marker(pr, "approved").get("at", 0),
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

    def reply(self, code, body, ctype="text/html; charset=utf-8"):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        if route == "/":
            err = verify("", "", exp, sig)
            if err:
                return self.deny(err)
            tab = (q.get("tab") or ["todo"])[0]
            srt = (q.get("sort") or ["newest"])[0]
            return self.index_page(tab if tab in dict(TABS) else "todo", srt)
        if not pr.isdigit():
            return self.reply(400, shell("Bad request", "<h1>Missing PR number.</h1>"))
        if route == "/pr":
            err = verify("pr", pr, exp, sig)
            return self.deny(err) if err else self.detail_page(pr)
        if route in ("/archive", "/unarchive"):
            if err := verify(route[1:], pr, exp, sig):
                return self.deny(err)
            f = STATE / pr / "archived"
            STATE.joinpath(pr).mkdir(parents=True, exist_ok=True)
            if route == "/archive":
                f.write_text(str(int(time.time())))
            else:
                f.unlink(missing_ok=True)
            return self.redirect(link("", ""))
        if route == "/review":
            # Accept either token: Slack cards carry a "review" token, the dashboard's own
            # Re-run button carries a "pr" one.
            if verify("review", pr, exp, sig) and (err := verify("pr", pr, exp, sig)):
                return self.deny(err)
            return self.start_review(pr, force=True)
        return self.reply(404, shell("Not found", "<h1>Not found.</h1>"))

    # -- POST --------------------------------------------------------------------------------
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        route = urlparse(self.path).path.rstrip("/").removeprefix("/prbot")
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
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
            return self.do_post_comments(pr, form)
        if action == "markdone":
            (STATE / pr).mkdir(parents=True, exist_ok=True)
            (STATE / pr / "approved").write_text(json.dumps(
                {"at": int(time.time()), "manual": True,
                 "body": "Handled outside the bot — approved on GitHub directly."}))
            return self.redirect(link("pr", pr))
        return self.do_approve(pr, form)

    # -- pages -------------------------------------------------------------------------------
    def index_page(self, tab="todo", sort="newest"):
        entries = []
        for item, active in all_prs():
            num = str(item.get("number"))
            st = pr_state(num)
            rev = load_review(num)
            t = pr_times(num)
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
            base = link("", "")
            for k, v in kw.items():
                base += f"&{k}={v}"
            return base

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
        body = (f"<h1>PR review queue</h1>"
                f"<p class='muted sm'>Reviewing as <code>{REVIEWER}</code>"
                + ("" if DRY_RUN else " · <b>live</b> — posting and approving write to GitHub")
                + f"</p>{dry_banner()}"
                  f"<div class=tabs>{tabs}</div>"
                  f"<div class=sortbar><span class='muted sm'>Sort</span>{sorts}</div>"
                  f"<div class=list>"
                + ("".join(rows) or f"<div class='rowlink muted'>{empty}</div>")
                + "</div>")
        return self.reply(200, shell("PR review queue", body))

    def timeline(self, pr):
        """reviewed → posted → approved, with dates. Reads at a glance where a PR stands."""
        t = pr_times(pr)
        posted = marker(pr, "posted.json")
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

    def detail_page(self, pr, banner=""):
        st = pr_state(pr)
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
                + ("" if active else "<span>· no longer awaiting your review</span>")
                + "</div>" + self.timeline(pr) + banner)

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
            approved = (self.approved_card(pr) if marker(pr, "approved").get("at") else "")
            body = (f"<div class=card><h4>Not reviewed here</h4>"
                    f"<p class='muted sm'>No review has been run for this PR on this box.</p>"
                    f"<p><a class='btn primary' href='{link('review', pr)}'>Run review</a></p>"
                    f"</div>") if not approved else approved
            return self.reply(200, shell(f"#{pr}",
                                         head + note + body + self.footer_actions(pr)))

        return self.reply(200, shell(f"#{pr}", head + self.review_body(pr, rev)))

    def review_body(self, pr, rev):
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

        posted = (STATE / pr / "posted.json").exists()
        posted_note = ("<div class='banner ok'><span>✓</span><div>Already posted to GitHub. "
                       "Posting again adds a second review.</div></div>" if posted else "")
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
        if marker(pr, "approved").get("at"):
            parts.append(self.approved_card(pr))
            parts.append(self.footer_actions(pr))
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
            + ("Approve (dry run)" if DRY_RUN else f"Approve #{pr} as {REVIEWER}")
            + "</button></p></form>"
              "<p class='muted sm'>Checks the PR is open, is not yours, and was reviewed "
              "here. This token lasts 30 minutes — reload if it lapses.</p>"
              "</div>")
        parts.append(self.footer_actions(pr))
        return "".join(parts)

    def approved_card(self, pr):
        appr = marker(pr, "approved")
        said = appr.get("body", "")
        manual = appr.get("manual")
        return (f"<h2>Approved</h2><div class=card>"
                f"<div class='banner ok'><span>✅</span><div><b>"
                + ("Marked as approved" if manual else "Approved")
                + f" on {fmt_date(appr['at'])}</b> ({ago(appr['at'])})"
                + ("" if manual else f" as <code>{REVIEWER}</code>")
                + ("<br>Recorded here only — the approval itself was done on GitHub."
                   if manual else "") + "</div></div>"
                + (f"<p class='muted sm'>Comment posted with the approval:</p>"
                   f"<pre><code>{html.escape(said)}</code></pre>"
                   if said and not manual else "")
                + f"<p><a class=btn href='{ghurl_of(pr)}'>View on GitHub</a></p></div>")

    def footer_actions(self, pr):
        """Local-only housekeeping: never touches GitHub."""
        exp_m, sig_m = mint("markdone", pr, ACTION_TTL)
        st = pr_state(pr)
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
    def start_review(self, pr, force=False):
        d = STATE / pr
        d.mkdir(parents=True, exist_ok=True)
        cur = (d / "status").read_text().strip() if (d / "status").exists() else ""
        idle = not cur or cur.startswith(("done", "posted", "dry-run", "failed"))
        if idle and (force or not cur):
            (d / "status").write_text("queued")
            with open(d / "run.log", "ab") as log:
                subprocess.Popen([str(BIN / "run-review.sh"), pr], stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        return self.redirect(link("pr", pr))

    def do_post_comments(self, pr, form):
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
            return self.detail_page(pr, "<div class='banner warn'><span>⚠️</span><div>Nothing "
                                        "selected — nothing sent.</div></div>")

        # Re-validate anchors against the CURRENT diff: the PR may have gained commits while
        # this review sat in the dashboard, and one stale line 422s the whole review.
        files, err = fetch_pr_files(pr)
        if err is not None:
            return self.detail_page(pr, (
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
        (STATE / pr / "payload.json").write_text(json.dumps(payload))

        if DRY_RUN:
            return self.detail_page(pr, (
                f"<div class='banner warn'><span>🧪</span><div><b>DRY RUN — nothing was sent "
                f"to GitHub.</b><br>Would post {len(inline)} inline comment(s)"
                + (f", {len(orphans)} folded into the summary" if orphans else "")
                + f", as <code>{event}</code>. Set <code>DRY_RUN=0</code> and restart "
                  f"<code>prbot</code> to post for real.</div></div>"))

        r = gh(["api", "--method", "POST", f"repos/{REPO}/pulls/{pr}/reviews",
                "--input", str(STATE / pr / "payload.json")])
        if r.returncode != 0:
            return self.detail_page(pr, f"<div class='banner err'><span>🔴</span><div>GitHub "
                                        f"rejected it: <code>"
                                        f"{html.escape(r.stderr[:400])}</code></div></div>")
        (STATE / pr / "posted.json").write_text(
            json.dumps({"at": int(time.time()), "inline": len(inline), "event": event}))
        return self.detail_page(pr, (
            f"<div class='banner ok'><span>✓</span><div>Posted {len(inline)} comment(s) as "
            f"<code>{event}</code> under <code>{REVIEWER}</code>."
            + (f" {len(orphans)} could not be anchored and went into the summary."
               if orphans else "") + "</div></div>"))

    def do_approve(self, pr, form):
        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
        rev = load_review(pr) or {}
        blockers = sev_counts(rev.get("comments", [])).get("blocker", 0)
        lgtm = blockers == 0 and rev.get("event") != "REQUEST_CHANGES"
        if not lgtm and not one("ack"):
            return self.detail_page(pr, "<div class='banner warn'><span>⚠️</span><div>This "
                                        "review is not LGTM — tick the confirmation to "
                                        "approve anyway.</div></div>")
        msg = one("approve_body").strip() or "LGTM."
        ok, why = can_approve(pr)
        if not ok:
            return self.detail_page(pr, f"<div class='banner err'><span>🚫</span><div>"
                                        f"{html.escape(why)}</div></div>")
        if DRY_RUN:
            return self.detail_page(pr, (
                f"<div class='banner warn'><span>🧪</span><div><b>DRY RUN — not approved.</b>"
                f"<br>Would submit an APPROVE review as <code>{REVIEWER}</code> with body: "
                f"<em>{html.escape(msg[:200])}</em></div></div>"))
        r = gh(["api", "--method", "POST", f"repos/{REPO}/pulls/{pr}/reviews",
                "-f", "event=APPROVE", "-f", f"body={msg}"])
        if r.returncode != 0:
            return self.detail_page(pr, f"<div class='banner err'><span>🔴</span><div>GitHub "
                                        f"rejected it: <code>"
                                        f"{html.escape(r.stderr[:400])}</code></div></div>")
        (STATE / pr / "approved").write_text(
            json.dumps({"at": int(time.time()), "body": msg}))
        return self.detail_page(pr, f"<div class='banner ok'><span>✅</span><div>Approved #{pr}"
                                    f" as <code>{REVIEWER}</code>.</div></div>")


if __name__ == "__main__":
    port = int(os.environ.get("PRBOT_PORT", "8899"))
    print(f"prbot listening on 127.0.0.1:{port} (dry_run={DRY_RUN})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
