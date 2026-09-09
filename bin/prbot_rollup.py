"""Phase 4 — trust-dashboard rollup, aggregated from files Robin already writes, ALL-TIME.

No new instrumentation: it walks per-PR/per-user state (run history), learnings.jsonl (keep-rate),
usage.json (tokens) and the Phase-3 agreement indices. Some metrics only have data from when their
feature shipped (tokens, keep-rate, agreement); those are marked as such in the UI. Pure function.
"""
import json
import time
from pathlib import Path

WEEK = 7 * 24 * 3600


def _mtime(p):
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return 0


def _tokens(p):
    try:
        u = json.loads(p.read_text())
        return int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _median(xs):
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


def compute(state, root, now=None):
    state, root = Path(state), Path(root)
    now = now or int(time.time())
    since = now - WEEK
    reviewers = {}                       # login -> {runs, week, tokens}
    prs = set()
    total_runs = week_runs = total_tokens = week_tokens = 0
    cycle = []                           # posted_at - requested_at, seconds

    if state.is_dir():
        for prd in state.iterdir():
            if not (prd.is_dir() and prd.name.isdigit()):
                continue
            ud = prd / "users"
            if not ud.is_dir():
                continue
            for d in ud.iterdir():
                if not d.is_dir():
                    continue
                rv = d / "review.json"
                runs = [_mtime(rv)] if rv.exists() else []
                hd = d / "history"
                if hd.is_dir():
                    runs += [int(h.name) for h in hd.iterdir()
                             if h.is_dir() and h.name.isdigit()]
                if not runs:
                    continue
                prs.add(prd.name)
                r = reviewers.setdefault(d.name, {"runs": 0, "week": 0, "tokens": 0})
                for t in runs:
                    total_runs += 1
                    r["runs"] += 1
                    if t >= since:
                        week_runs += 1
                        r["week"] += 1
                tok = _tokens(d / "usage.json")
                total_tokens += tok
                r["tokens"] += tok
                if _mtime(rv) >= since:
                    week_tokens += tok
                req, posted = d / "requested_at", d / "posted.json"
                if req.exists() and posted.exists():
                    try:
                        ra = int(req.read_text().strip())
                        pa = int(json.loads(posted.read_text()).get("at", 0))
                        if ra and pa >= ra:
                            cycle.append(pa - ra)
                    except (OSError, ValueError, TypeError):
                        pass

    # keep-rate from learnings.jsonl (only exists from when Learnings shipped)
    keep = {"kept": 0, "edited": 0, "dropped": 0}
    keep_week = {"kept": 0, "edited": 0, "dropped": 0}
    lf = root / "learnings.jsonl"
    if lf.exists():
        for line in lf.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            o = row.get("outcome")
            if o in keep:
                keep[o] += 1
                if row.get("at", 0) >= since:
                    keep_week[o] += 1

    # agreement from the Phase-3 per-head indices (forward-looking; older PRs have none)
    multi, confirmed, rates = 0, 0, []
    if state.is_dir():
        for prd in state.iterdir():
            ad = prd / "agreement"
            if not (prd.is_dir() and ad.is_dir()):
                continue
            files = sorted(ad.glob("*.json"), key=_mtime)
            if not files:
                continue
            try:
                a = json.loads(files[-1].read_text())
            except (OSError, ValueError):
                continue
            if len(a.get("runs", [])) >= 2:
                multi += 1
                confirmed += a.get("confirmed", 0)
                if a.get("rate") is not None:
                    rates.append(a["rate"])

    def krate(d):
        t = sum(d.values())
        return round(100 * d["kept"] / t, 1) if t else None

    return {
        "generatedAt": now,
        "reviews": {"total": total_runs, "week": week_runs},
        "prs": len(prs),
        "reviewers": sorted(([{"login": k, **v} for k, v in reviewers.items()]),
                            key=lambda r: -r["runs"]),
        "tokens": {"total": total_tokens, "week": week_tokens},
        "keep": {"allTime": {**keep, "rate": krate(keep)},
                 "week": {**keep_week, "rate": krate(keep_week)}},
        "agreement": {"multiReviewerPRs": multi, "confirmedFindings": confirmed,
                      "avgRate": (round(sum(rates) / len(rates), 1) if rates else None)},
        "cycle": {"medianReviewToPostSec": _median(cycle), "n": len(cycle)},
    }
