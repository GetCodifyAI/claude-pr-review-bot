import { useCallback, useEffect, useState } from "react";
import { api, type StackData } from "./api";
import { Link, useLocation } from "./router";

function Pill({ kind }: { kind: string }) {
  return <span className={"pill " + kind}>{kind}</span>;
}

export function StackPage() {
  const { search } = useLocation();
  const pr = search.get("pr") || "";
  const [d, setD] = useState<StackData | null>(null);
  const [effort, setEffort] = useState("standard");
  const [started, setStarted] = useState<number | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const load = useCallback(() => (pr ? api.stack(pr).then(setD) : Promise.resolve()), [pr]);
  useEffect(() => {
    load();
  }, [load]);
  // default: every PR in the stack selected
  useEffect(() => {
    if (d) setSel(new Set(d.stack.map((it) => it.num)));
  }, [d]);
  const toggle = (num: string) =>
    setSel((prev) => {
      const next = new Set(prev);
      next.has(num) ? next.delete(num) : next.add(num);
      return next;
    });

  const head = (
    <>
      <nav className="bc">
        <Link to="/">Queue</Link>
        <span className="sep">/</span>
        <Link to={`/pr?pr=${pr}`}>#{pr}</Link>
        <span className="sep">/</span>
        <span className="cur">stack</span>
      </nav>
      <h1 className="prtitle">Stacked review</h1>
    </>
  );

  if (!d) return <>{head}<div className="muted">Loading…</div></>;

  if (!d.isStack)
    return (
      <>
        {head}
        <div className="card top">
          <h4 style={{ marginTop: 0 }}>Not a stack</h4>
          <p className="muted sm">
            This PR isn't stacked on another open PR — its base branch isn't another open PR's
            branch. <Link to={`/pr?pr=${pr}`}>Back to the review</Link>.
          </p>
        </div>
      </>
    );

  const gate = (
    <div className="claudegate">
      <div className="cg-ico">✳</div>
      <div className="cg-body">
        <b>Connect your Claude account to run reviews</b>
        <p className="muted sm">Reviews run on your own Claude subscription.</p>
        <Link className="btn primary" to="/integrations">
          Connect Claude →
        </Link>
      </div>
    </div>
  );

  async function runSelected() {
    if (!d || sel.size === 0) return;
    const r = await api.stackRun(pr, d.runToken, effort, [...sel]);
    setStarted(r.started);
    load();
  }

  return (
    <>
      {head}
      <p className="lead">
        These open PRs form a stack (each based on the one above). Review the whole stack from here
        instead of triggering each separately.
      </p>
      <div className="list">
        {d.stack.map((it, i) => {
          const pos = i === 0 ? "top" : i === d.stack.length - 1 ? "bottom" : "";
          return (
            <div className="row stackrow" key={it.num}>
              <input
                type="checkbox"
                className="fsel"
                checked={sel.has(it.num)}
                onChange={() => toggle(it.num)}
                aria-label={`Select #${it.num}`}
              />
              <Link className="rowlink" to={`/pr?pr=${it.num}`}>
                <div className="rowtop">
                  <span className="num">#{it.num}</span>
                  <span className="ttl">{it.title}</span>
                </div>
                <div className="muted sm rowsub">
                  <span>
                    <code>{it.base}</code> ← <code>{it.head}</code>
                  </span>
                  {pos && <span>{pos} of stack</span>}
                </div>
              </Link>
              <div className="rowmeta">
                <Pill kind={it.state} />
                <Link className="chev" to={`/pr?pr=${it.num}`} aria-hidden="true">
                  ›
                </Link>
              </div>
            </div>
          );
        })}
      </div>
      <div className="card top">
        {started !== null && (
          <div className="banner ok">
            <span>✓</span>
            <div>
              Queued {started.toLocaleString()} review{started === 1 ? "" : "s"}. They run one at a
              time on the box.
            </div>
          </div>
        )}
        {d.connected ? (
          <>
            <div className="effort-lbl">Effort (applied to every PR in the stack)</div>
            <div className="effrow">
              {d.levels.map((lv) => (
                <label key={lv.key} className={"eff" + (effort === lv.key ? " hot" : "")}>
                  <input
                    type="radio"
                    name="effort"
                    checked={effort === lv.key}
                    onChange={() => setEffort(lv.key)}
                  />
                  <span className="effname">{lv.name}</span>
                  <span className="effsub">{lv.sub}</span>
                </label>
              ))}
            </div>
            <div className="runrow">
              <span className="hint" style={{ flex: 1 }}>
                Tick the PRs to review — each queues a review (skipping any already running); they
                run one at a time on the box.
              </span>
              <button
                className="btn primary"
                type="button"
                onClick={runSelected}
                disabled={sel.size === 0}
              >
                Review selected ({sel.size})
              </button>
            </div>
          </>
        ) : (
          gate
        )}
      </div>
    </>
  );
}
