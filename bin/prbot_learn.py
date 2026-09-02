"""prbot_learn.py — the "Learnings" loop: capture what reviewers reject, feed it back.

Every time a reviewer unticks a finding (not worth saying) or edits its wording before posting,
that is a labeled example the dashboard already produces and used to discard. record() logs it;
render() turns recent rejections into a compact block appended to the review prompt so the agent
stops re-raising the same noise; recent() backs the read-only /learnings page.

Storage: $ROOT/learnings.jsonl (ROOT defaults to ~/.claude-pr-bot, same as the server + shell).
Only short gists are stored — high signal, low bloat. Imported by prbot-server.py; run-review.sh
calls render() via `python3 -c`.
"""
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(os.environ.get("ROOT", Path.home() / ".claude-pr-bot"))
FILE = ROOT / "learnings.jsonl"
CAP = 300                          # keep only the most recent this many rows


def _gist(body, limit=120):
    """One-line plain-text gist of a finding body (mirrors the server's gist())."""
    t = re.sub(r"```.*?```", "", body or "", flags=re.S)
    t = re.sub(r"[`*_>#]", "", t).replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    cut = t.split(". ")[0].strip(" .")
    return (cut[:limit].rstrip() + "…") if len(cut) > limit else cut


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _read():
    rows = []
    if FILE.exists():
        for line in FILE.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def record(pr, user, originals_sorted, form):
    """Log the outcome of each original finding for one posted review.

    `originals_sorted` is review.json's comments sorted exactly as the dashboard renders them
    (by severity), so form index i lines up. `form` is the parsed POST body: sel_i present =>
    kept/edited, absent => dropped; body_i is the (possibly edited) text.
    """
    one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
    now = int(time.time())
    out = []
    for i, orig in enumerate(originals_sorted):
        ob = orig.get("body", "")
        selected = bool(form.get(f"sel_{i}"))
        edited_body = one(f"body_{i}")
        if not selected:
            outcome = "dropped"
        elif _norm(edited_body) != _norm(ob):
            outcome = "edited"
        else:
            outcome = "kept"
        row = {"at": now, "pr": str(pr), "user": user,
               "path": orig.get("path", ""), "line": orig.get("line"),
               "severity": orig.get("severity", "nit"),
               "gist": _gist(ob), "outcome": outcome}
        if outcome == "edited":
            row["edited_gist"] = _gist(edited_body)
        out.append(row)
    if not out:
        return
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        rows = _read() + out
        rows = rows[-CAP:]
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        os.chmod(tmp, 0o600)
        tmp.replace(FILE)
    except OSError:
        pass                       # a learning we fail to store must never break a post


def render(max_items=40):
    """A compact prompt block of recent dropped/edited findings, or "" if nothing learned.

    Appended to the review prompt so the agent weighs the reviewer's past decisions."""
    rows = [r for r in _read() if r.get("outcome") in ("dropped", "edited")]
    if not rows:
        return ""
    rows = rows[-max_items:]
    dropped = [r for r in rows if r["outcome"] == "dropped"]
    edited = [r for r in rows if r["outcome"] == "edited"]
    lines = ["\n\nReviewer preferences learned from past reviews on this repo — weigh these:"]
    if dropped:
        lines.append("\nFindings the reviewer chose NOT to post (treat near-duplicates as noise "
                     "and omit them unless clearly higher-stakes here):")
        for r in dropped[-24:]:
            loc = f"{r.get('path', '')}" + (f":{r['line']}" if r.get("line") else "")
            lines.append(f"- [{r.get('severity', 'nit')}] {loc} — {r.get('gist', '')}")
    if edited:
        lines.append("\nFindings the reviewer kept but reworded (prefer this tighter phrasing "
                     "and level of detail):")
        for r in edited[-12:]:
            lines.append(f"- was: {r.get('gist', '')}\n  became: {r.get('edited_gist', '')}")
    lines.append("\nThese are preferences, not rules — still raise a genuine, higher-severity "
                 "issue even if it resembles a past drop.")
    return "\n".join(lines)


def recent(n=60):
    """Most-recent rows first, for the /learnings page."""
    return list(reversed(_read()))[:n]


def counts():
    c = {"dropped": 0, "edited": 0, "kept": 0}
    for r in _read():
        o = r.get("outcome")
        if o in c:
            c[o] += 1
    return c


if __name__ == "__main__":       # `python3 -c` path used by run-review.sh prints the block
    print(render())
