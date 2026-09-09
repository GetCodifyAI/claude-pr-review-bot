import { useEffect, useState } from "react";
import { api, type RollupData } from "./api";

// Phase 4 — the trust dashboard. Pure aggregation of files Robin already writes, ALL-TIME.
// Leading indicators (usage, keep-rate, agreement) are separated from lagging ones (cycle time)
// so week-1 pilot data isn't oversold. Every number is traceable to an existing file.

function num(n: number): string {
  return n.toLocaleString("en-US");
}

function dur(sec: number | null): string {
  if (sec == null) return "—";
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)} hr`;
  return `${(sec / 86400).toFixed(1)} days`;
}

function Stat({ label, value, sub, kind }: { label: string; value: string; sub?: string; kind?: string }) {
  return (
    <div className={"rollcard" + (kind ? " " + kind : "")}>
      <div className="rolllabel">{label}</div>
      <div className="rollval">{value}</div>
      {sub && <div className="rollsub">{sub}</div>}
    </div>
  );
}

export function Rollup() {
  const [d, setD] = useState<RollupData | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    api.rollup().then(setD).catch(() => setErr(true));
  }, []);

  if (err) return <div className="card"><p className="muted">Couldn't load the rollup.</p></div>;
  if (!d) return <div className="wrap-load muted">Loading…</div>;

  const keep = d.keep.allTime;
  return (
    <>
      <h1>Trust dashboard</h1>
      <p className="muted sm">
        All-time, from day one — aggregated from what Robin already records. Leading indicators
        (usage, keep-rate, agreement) come first; the lagging one (cycle time) is framed separately.
        Read these as early signal to build on, not proof.
      </p>

      <div className="rollhead">Leading — usage</div>
      <div className="rollgrid">
        <Stat label="Reviews run" value={num(d.reviews.total)} sub={`${num(d.reviews.week)} in the last 7 days`} />
        <Stat label="PRs reviewed" value={num(d.prs)} />
        <Stat label="Reviewers" value={num(d.reviewers.length)} />
        <Stat
          label="Tokens spent"
          value={num(d.tokens.total)}
          sub={`${num(d.tokens.week)} this week · captured runs only`}
        />
      </div>

      <div className="rollhead">Leading — precision</div>
      <div className="rollgrid">
        <Stat
          label="Findings kept as-is"
          value={keep.rate == null ? "—" : `${keep.rate}%`}
          sub={`${num(keep.kept)} kept · ${num(keep.edited)} edited · ${num(keep.dropped)} dropped`}
          kind="good"
        />
        <Stat
          label="Agreement across reviewers"
          value={d.agreement.avgRate == null ? "—" : `${d.agreement.avgRate}%`}
          sub={`${num(d.agreement.multiReviewerPRs)} multi-reviewer PRs · ${num(
            d.agreement.confirmedFindings,
          )} confirmed findings`}
          kind="accent"
        />
      </div>
      <p className="muted sm rollnote">
        Agreement is <b>independence-weighted</b>: a finding counts as confirmed only when reviewers
        using a different skill/model/effort raised it. It's a precision signal to improve toward —
        and because reviewers are nudged to look at <i>different</i> things, a lower number can mean
        broader coverage, not worse reviews.
      </p>

      <div className="rollhead">Lagging — cycle time</div>
      <div className="rollgrid">
        <Stat
          label="Median review → first comment posted"
          value={dur(d.cycle.medianReviewToPostSec)}
          sub={
            d.cycle.n
              ? `over ${num(d.cycle.n)} posted review(s)`
              : "needs requested-at data (captured from now on)"
          }
        />
      </div>

      <div className="rollhead">By reviewer</div>
      <div className="card rolltablewrap">
        <table className="rolltable">
          <thead>
            <tr>
              <th>Reviewer</th>
              <th>Reviews (all-time)</th>
              <th>Last 7 days</th>
              <th>Tokens</th>
            </tr>
          </thead>
          <tbody>
            {d.reviewers.length === 0 ? (
              <tr>
                <td colSpan={4} className="muted">No reviews recorded yet.</td>
              </tr>
            ) : (
              d.reviewers.map((r) => (
                <tr key={r.login}>
                  <td>{r.login}</td>
                  <td>{num(r.runs)}</td>
                  <td>{num(r.week)}</td>
                  <td>{num(r.tokens)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
