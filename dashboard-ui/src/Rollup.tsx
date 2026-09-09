import { useEffect, useMemo, useState } from "react";
import { api, type RollupData, type RollupSeriesPoint } from "./api";

// Insights — Robin's activity, precision and agreement, aggregated from files it already writes.
// All charts are hand-rolled SVG (no chart dependency), matching Robin's no-framework style.

const C = {
  accent: "#7c83f0",
  green: "#34a86e",
  amber: "#d9a441",
  red: "#e5658a",
  blue: "#4c9be8",
  dim: "#8a8f98",
  faint: "#3a3f4a",
};

function num(n: number): string {
  return n.toLocaleString("en-US"); // org rule: commas in numbers
}
function pct(n: number | null): string {
  return n == null ? "—" : `${n.toFixed(1)}%`; // org rule: one decimal
}
function dur(sec: number | null): string {
  if (sec == null) return "—";
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)} hr`;
  return `${(sec / 86400).toFixed(1)} days`;
}

// ---- charts ---------------------------------------------------------------------------------

function BarChart({ points, color }: { points: RollupSeriesPoint[]; color: string }) {
  const w = 720;
  const h = 150;
  const pad = 22;
  const n = points.length;
  const max = Math.max(1, ...points.map((p) => p.reviews));
  const bw = (w - pad * 2) / n;
  const ticks = [0, Math.floor(n / 2), n - 1];
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="chart" preserveAspectRatio="none" role="img"
         aria-label="Reviews per day">
      <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} stroke={C.faint} strokeWidth={1} />
      {points.map((p, i) => {
        const bh = (p.reviews / max) * (h - pad * 2);
        return (
          <rect key={i} x={pad + i * bw + bw * 0.15} y={h - pad - bh}
                width={Math.max(1, bw * 0.7)} height={bh} rx={1.5} fill={color}>
            <title>{`${p.date}: ${p.reviews} review(s)`}</title>
          </rect>
        );
      })}
      {ticks.map((t) => (
        <text key={t} x={pad + t * bw + bw / 2} y={h - 6} fontSize={9} fill={C.dim}
              textAnchor="middle">{points[t]?.date}</text>
      ))}
      <text x={pad} y={13} fontSize={9} fill={C.dim}>max {max}/day</text>
    </svg>
  );
}

function Donut({ segments, center, sub }:
  { segments: { label: string; value: number; color: string }[]; center: string; sub: string }) {
  const total = segments.reduce((a, s) => a + s.value, 0);
  const r = 52;
  const cx = 66;
  const cy = 66;
  const circ = 2 * Math.PI * r;
  let offset = 0;
  return (
    <div className="donutwrap">
      <svg viewBox="0 0 132 132" width={132} height={132}>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke={C.faint} strokeWidth={16} />
        {total > 0 &&
          segments.map((s, i) => {
            const len = (s.value / total) * circ;
            const el = (
              <circle key={i} cx={cx} cy={cy} r={r} fill="none" stroke={s.color} strokeWidth={16}
                      strokeDasharray={`${len} ${circ - len}`} strokeDashoffset={-offset}
                      transform={`rotate(-90 ${cx} ${cy})`}>
                <title>{`${s.label}: ${s.value}`}</title>
              </circle>
            );
            offset += len;
            return el;
          })}
        <text x={cx} y={cy - 2} textAnchor="middle" fontSize={22} fontWeight={700} fill="#e8eaf0">
          {center}
        </text>
        <text x={cx} y={cy + 16} textAnchor="middle" fontSize={9} fill={C.dim}>{sub}</text>
      </svg>
      <div className="legend">
        {segments.map((s) => (
          <div key={s.label} className="legrow">
            <span className="legdot" style={{ background: s.color }} />
            {s.label} <b>{num(s.value)}</b>
          </div>
        ))}
      </div>
    </div>
  );
}

function HBars({ rows, color }: { rows: { label: string; value: number; note?: string }[]; color: string }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <div className="hbars">
      {rows.length === 0 && <div className="muted sm">No data yet.</div>}
      {rows.map((r) => (
        <div key={r.label} className="hbar">
          <div className="hbar-label" title={r.label}>{r.label}</div>
          <div className="hbar-track">
            <div className="hbar-fill" style={{ width: `${(r.value / max) * 100}%`, background: color }} />
          </div>
          <div className="hbar-val">{r.note ?? num(r.value)}</div>
        </div>
      ))}
    </div>
  );
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="kpi">
      <div className="kpi-l">{label}</div>
      <div className="kpi-v">{value}</div>
      {sub && <div className="kpi-s">{sub}</div>}
    </div>
  );
}

// ---- page -----------------------------------------------------------------------------------

const RANGES: [string, number][] = [
  ["7 days", 7],
  ["30 days", 30],
  ["90 days", 90],
];

export function Rollup() {
  const [d, setD] = useState<RollupData | null>(null);
  const [err, setErr] = useState(false);
  const [range, setRange] = useState(30);

  useEffect(() => {
    api.rollup().then(setD).catch(() => setErr(true));
  }, []);

  const period = useMemo(() => {
    if (!d) return null;
    const pts = d.series.slice(-range);
    const reviews = pts.reduce((a, p) => a + p.reviews, 0);
    const tokens = pts.reduce((a, p) => a + p.tokens, 0);
    const kept = pts.reduce((a, p) => a + p.kept, 0);
    const edited = pts.reduce((a, p) => a + p.edited, 0);
    const dropped = pts.reduce((a, p) => a + p.dropped, 0);
    const kt = kept + edited + dropped;
    return { pts, reviews, tokens, kept, edited, dropped, keepRate: kt ? (100 * kept) / kt : null };
  }, [d, range]);

  if (err) return <div className="card"><p className="muted">Couldn't load insights.</p></div>;
  if (!d || !period) return <div className="wrap-load muted">Loading…</div>;

  const sev = d.severity;
  return (
    <>
      <div className="insights-head">
        <div>
          <h1>Insights</h1>
          <p className="muted sm">
            Robin's activity, precision and agreement — all-time, from day one. Read these as early
            signal to build on, not proof.
          </p>
        </div>
        <div className="rangepills">
          {RANGES.map(([label, days]) => (
            <button key={days} type="button"
                    className={"rangepill" + (range === days ? " on" : "")}
                    onClick={() => setRange(days)}>
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="kpirow">
        <Kpi label={`Reviews · last ${range}d`} value={num(period.reviews)}
             sub={`${num(d.reviews.total)} all-time`} />
        <Kpi label={`Tokens · last ${range}d`} value={num(period.tokens)}
             sub={`${num(d.tokens.total)} all-time · captured runs`} />
        <Kpi label={`Kept as-is · last ${range}d`} value={pct(period.keepRate)}
             sub={`${pct(d.keep.allTime.rate)} all-time`} />
        <Kpi label="PRs · reviewers" value={`${num(d.prs)} · ${num(d.reviewers.length)}`}
             sub="distinct PRs · people" />
      </div>

      <div className="panel">
        <div className="panel-h">Review activity — per day (last {range} days)</div>
        <BarChart points={period.pts} color={C.accent} />
      </div>

      <div className="grid2">
        <div className="panel">
          <div className="panel-h">Findings kept vs. edited vs. dropped (last {range}d)</div>
          <Donut
            center={pct(period.keepRate)}
            sub="kept"
            segments={[
              { label: "Kept", value: period.kept, color: C.green },
              { label: "Edited", value: period.edited, color: C.amber },
              { label: "Dropped", value: period.dropped, color: C.red },
            ]}
          />
        </div>
        <div className="panel">
          <div className="panel-h">Findings by severity (all-time)</div>
          <HBars
            color={C.blue}
            rows={[
              { label: "Blocker", value: sev.blocker },
              { label: "Should-fix", value: sev["should-fix"] },
              { label: "Nit", value: sev.nit },
              { label: "Question", value: sev.question },
            ]}
          />
        </div>
      </div>

      <div className="grid2">
        <div className="panel">
          <div className="panel-h">By reviewer (all-time runs)</div>
          <HBars color={C.accent}
                 rows={d.reviewers.map((r) => ({ label: r.login, value: r.runs }))} />
        </div>
        <div className="panel">
          <div className="panel-h">By model</div>
          <HBars color={C.green}
                 rows={d.models.map((m) => ({ label: m.model, value: m.runs,
                                              note: `${num(m.runs)} · ${num(m.tokens)} tok` }))} />
        </div>
      </div>

      <div className="grid2">
        <div className="panel">
          <div className="panel-h">Agreement across reviewers</div>
          <div className="agreebig">{pct(d.agreement.avgRate)}</div>
          <p className="muted sm">
            {num(d.agreement.multiReviewerPRs)} multi-reviewer PRs · {num(d.agreement.confirmedFindings)}{" "}
            confirmed findings. <b>Independence-weighted</b>: counts only when reviewers using a
            different skill/model/effort agreed. A precision signal to improve toward — a lower
            number can mean broader coverage, not worse reviews.
          </p>
        </div>
        <div className="panel">
          <div className="panel-h">Cycle time (lagging)</div>
          <div className="agreebig">{dur(d.cycle.medianReviewToPostSec)}</div>
          <p className="muted sm">
            Median review → first comment posted{d.cycle.n ? ` over ${num(d.cycle.n)} posted review(s)` : ""}.
            {d.cycle.n ? "" : " Needs requested-at data — captured from now on."}
          </p>
        </div>
      </div>
    </>
  );
}
