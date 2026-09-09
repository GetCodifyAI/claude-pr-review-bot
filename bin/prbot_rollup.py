"""Insights rollup — aggregated from files Robin already writes, ALL-TIME, from day one.

No new instrumentation: walks per-PR/per-user state (run history), learnings.jsonl (keep-rate),
usage.json (tokens + model), and the agreement indices. Returns headline totals, a per-day series
(for charts + a client-side range filter), and structural breakdowns (by reviewer, severity,
model). Some metrics only have data from when their feature shipped; the UI labels those. Pure fn.
"""
import json
import time
from pathlib import Path

WEEK = 7 * 24 * 3600
DAY = 24 * 3600
SERIES_DAYS = 90


def _mtime(p):
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return 0


def _load(p):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _tokens(u):
    if not isinstance(u, dict):
        return 0
    return int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))


def _median(xs):
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


def _daystart(ts):
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def compute(state, root, now=None):
    state, root = Path(state), Path(root)
    now = now or int(time.time())
    since = now - WEEK

    reviewers = {}
    prs = set()
    total_runs = week_runs = total_tokens = week_tokens = 0
    cycle = []
    severity = {"blocker": 0, "should-fix": 0, "nit": 0, "question": 0}
    models = {}                                  # model -> {runs, tokens}
    day = {}                                     # daystart_ts -> {reviews, tokens}

    def bump_day(ts, reviews=0, tokens=0):
        d = day.setdefault(_daystart(ts), {"reviews": 0, "tokens": 0})
        d["reviews"] += reviews
        d["tokens"] += tokens

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
                    bump_day(t, reviews=1)
                    if t >= since:
                        week_runs += 1
                        r["week"] += 1
                tok = _tokens(_load(d / "usage.json"))
                total_tokens += tok
                r["tokens"] += tok
                bump_day(_mtime(rv), tokens=tok)
                if _mtime(rv) >= since:
                    week_tokens += tok
                # model + severity from the current review
                u = _load(d / "usage.json")
                if isinstance(u, dict) and u.get("model"):
                    m = models.setdefault(u["model"], {"runs": 0, "tokens": 0})
                    m["runs"] += 1
                    m["tokens"] += _tokens(u)
                rev = _load(rv)
                if isinstance(rev, dict):
                    for c in rev.get("comments", []) or []:
                        sev = c.get("severity", "nit")
                        if sev in severity:
                            severity[sev] += 1
                req, posted = d / "requested_at", d / "posted.json"
                if req.exists() and posted.exists():
                    try:
                        ra = int(req.read_text().strip())
                        pa = int((_load(posted) or {}).get("at", 0))
                        if ra and pa >= ra:
                            cycle.append(pa - ra)
                    except (OSError, ValueError, TypeError):
                        pass

    # keep-rate (all-time) + per-day keep buckets, from learnings.jsonl
    keep = {"kept": 0, "edited": 0, "dropped": 0}
    keep_day = {}
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
                kd = keep_day.setdefault(_daystart(row.get("at", now)),
                                         {"kept": 0, "edited": 0, "dropped": 0})
                kd[o] += 1

    # agreement (forward-looking)
    multi, confirmed, rates = 0, 0, []
    if state.is_dir():
        for prd in state.iterdir():
            ad = prd / "agreement"
            if not (prd.is_dir() and ad.is_dir()):
                continue
            files = sorted(ad.glob("*.json"), key=_mtime)
            a = _load(files[-1]) if files else None
            if a and len(a.get("runs", [])) >= 2:
                multi += 1
                confirmed += a.get("confirmed", 0)
                if a.get("rate") is not None:
                    rates.append(a["rate"])

    # per-day series for the last SERIES_DAYS (zero-filled), newest last
    series = []
    d0 = _daystart(now)
    for i in range(SERIES_DAYS - 1, -1, -1):
        ts = d0 - i * DAY
        dd = day.get(ts, {})
        kk = keep_day.get(ts, {})
        series.append({"ts": ts, "date": time.strftime("%m/%d/%y", time.localtime(ts)),
                       "reviews": dd.get("reviews", 0), "tokens": dd.get("tokens", 0),
                       "kept": kk.get("kept", 0), "edited": kk.get("edited", 0),
                       "dropped": kk.get("dropped", 0)})

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
        "keep": {"allTime": {**keep, "rate": krate(keep)}},
        "severity": severity,
        "models": sorted(([{"model": k, **v} for k, v in models.items()]),
                         key=lambda m: -m["runs"]),
        "agreement": {"multiReviewerPRs": multi, "confirmedFindings": confirmed,
                      "avgRate": (round(sum(rates) / len(rates), 1) if rates else None)},
        "cycle": {"medianReviewToPostSec": _median(cycle), "n": len(cycle)},
        "series": series,
    }
