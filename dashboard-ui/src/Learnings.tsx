import { useEffect, useState } from "react";
import { api, type LearningsData, type Me } from "./api";

function Pill({ kind, label }: { kind: string; label?: string }) {
  return <span className={"pill " + kind}>{label || kind}</span>;
}

export function Learnings({ me }: { me: Me }) {
  const [d, setD] = useState<LearningsData | null>(null);
  useEffect(() => {
    api.learnings().then(setD);
  }, []);
  if (!d) return <div className="muted">Loading…</div>;

  return (
    <>
      <h1>What {me.brand} has learned</h1>
      <p className="lead">
        Every time you drop a finding as noise or reword one before posting, {me.brand} remembers it
        and weighs it on the next review of this repo — so it stops repeating what you reject. This
        is that memory.
      </p>
      <div className="stats">
        <a className="stat hot">
          <div className="k">{d.counts.dropped.toLocaleString()}</div>
          <div className="l">Dropped as noise</div>
        </a>
        <a className="stat">
          <div className="k">{d.counts.edited.toLocaleString()}</div>
          <div className="l">Reworded</div>
        </a>
        <a className="stat">
          <div className="k">{d.counts.kept.toLocaleString()}</div>
          <div className="l">Kept as-is</div>
        </a>
      </div>
      {d.rows.length === 0 ? (
        <div className="empty">
          <span className="ic">🧠</span>
          <b>Nothing learned yet</b>
          Post or drop a few findings and they'll show up here.
        </div>
      ) : (
        <>
          <h2>Recent decisions</h2>
          <div className="list">
            {d.rows.map((r, i) => (
              <div className="row" key={i}>
                <div className="rowlink">
                  <div className="rowtop">
                    <Pill kind={r.kind} label={r.label} />
                    <span className="loc">{r.loc}</span>
                    <Pill kind={r.severity} />
                  </div>
                  <div className="muted sm" style={{ marginTop: 5 }}>
                    {r.gist}
                  </div>
                  {r.editedGist && (
                    <div className="muted sm" style={{ marginTop: 4 }}>
                      → {r.editedGist}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
          <p className="fine">
            Shared across the team for this repo. These are preferences, not hard rules — {me.brand}{" "}
            still raises a genuine higher-severity issue even if it resembles a past drop.
          </p>
        </>
      )}
    </>
  );
}
