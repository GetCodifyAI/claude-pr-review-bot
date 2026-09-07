import { useCallback, useEffect, useRef, useState } from "react";
import { api, type QaDetail } from "./api";
import { Md } from "./Md";
import { Link, navigate, useLocation } from "./router";

function QaIndex() {
  const [guides, setGuides] = useState<{ num: string; title: string; when: string }[]>([]);
  const [pr, setPr] = useState("");
  useEffect(() => {
    api.qaIndex().then((d) => setGuides(d.guides));
  }, []);
  return (
    <>
      <h1>QA guides</h1>
      <p className="lead">
        Generate a tester-ready QA guide for a PR — risk-tiered manual test cases, setup steps, a
        surface matrix and what <em>not</em> to file — all grounded in the real diff. Then hand it
        straight to QA.
      </p>
      <div className="card">
        <h4 style={{ marginTop: 0 }}>Generate a guide</h4>
        <form
          className="qagen"
          onSubmit={(e) => {
            e.preventDefault();
            if (/^\d+$/.test(pr.trim())) navigate(`/qa?pr=${pr.trim()}`);
          }}
        >
          <input
            className="in"
            inputMode="numeric"
            pattern="[0-9]+"
            autoComplete="off"
            placeholder="PR number — e.g. 38849"
            value={pr}
            onChange={(e) => setPr(e.target.value)}
          />
          <button className="btn primary" type="submit">
            Open
          </button>
        </form>
        <div className="hint">Enter a PR number to view its guide or generate a new one.</div>
      </div>
      {guides.length > 0 ? (
        <>
          <h2>Recent guides</h2>
          <div className="list">
            {guides.map((g) => (
              <div className="row" key={g.num}>
                <Link className="rowlink" to={`/qa?pr=${g.num}`}>
                  <div className="rowtop">
                    <span className="num">#{g.num}</span>
                    <span className="ttl">{g.title}</span>
                  </div>
                  <div className="muted sm rowsub">
                    <span>guide ready · {g.when}</span>
                  </div>
                </Link>
                <div className="rowmeta">
                  <Link className="chev" to={`/qa?pr=${g.num}`} aria-hidden="true">
                    ›
                  </Link>
                </div>
              </div>
            ))}
          </div>
        </>
      ) : (
        <div className="empty">
          <span className="ic">🧪</span>
          <b>No guides yet</b>
          Enter a PR number above to build the first one.
        </div>
      )}
    </>
  );
}

function QaDetailView({ pr }: { pr: string }) {
  const [d, setD] = useState<QaDetail | null>(null);
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const load = useCallback(() => api.qaDetail(pr).then(setD), [pr]);

  useEffect(() => {
    setD(null);
    load();
  }, [load]);
  useEffect(() => {
    window.clearInterval(timer.current);
    if (d && d.state === "running") timer.current = window.setInterval(load, 4000);
    return () => window.clearInterval(timer.current);
  }, [d, load]);

  if (!d) return <div className="muted">Loading…</div>;

  const header = (
    <>
      <nav className="bc">
        <Link to="/qa">QA guides</Link>
        <span className="sep">/</span>
        <span className="cur">#{pr}</span>
      </nav>
      <h1 className="prtitle">
        #{pr} — {d.title}
      </h1>
      <div className="meta">
        <a href={d.ghUrl} target="_blank" rel="noopener">
          open on GitHub
        </a>
      </div>
    </>
  );

  const gate = (
    <div className="claudegate">
      <div className="cg-ico">✳</div>
      <div className="cg-body">
        <b>Connect your Claude account to generate a QA guide</b>
        <p className="muted sm">Generating a QA guide runs on your own Claude subscription.</p>
        <Link className="btn primary" to="/integrations">
          Connect Claude →
        </Link>
      </div>
    </div>
  );

  async function gen() {
    if (!d) return;
    await api.qaGen(pr, d.genToken);
    load();
  }
  async function stop() {
    if (!d?.stopToken) return;
    await api.qaStop(pr, d.stopToken);
    load();
  }
  async function copy() {
    if (!d?.md) return;
    await navigator.clipboard.writeText(d.md);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  }

  if (d.state === "running" && d.running) {
    const r = d.running;
    return (
      <>
        {header}
        <div className="card top">
          <div className="prog-hd">
            Building QA guide for <b>#{pr}</b>
          </div>
          <ul className="prog">
            {r.phases.map((ph, j) => (
              <li key={ph} className={j < r.cur ? "done" : j === r.cur ? "now" : ""}>
                <span className={"pm" + (j === r.cur ? " spin" : "")}>
                  {j < r.cur ? "✓" : j === r.cur ? "" : "○"}
                </span>
                {ph}
              </li>
            ))}
          </ul>
          <div className="progbar">
            <div className="progfill" />
          </div>
          {r.queued && <div className="hint">Waiting for another job to finish first.</div>}
          <div className="hint" style={{ marginTop: 10 }}>
            This page refreshes itself; reading the diff and review history takes a few minutes.
          </div>
          <form style={{ marginTop: 12 }} onSubmit={(e) => { e.preventDefault(); stop(); }}>
            <button className="btn soft" type="submit">
              Stop
            </button>
          </form>
        </div>
      </>
    );
  }

  if (d.state === "done" && d.md) {
    return (
      <>
        {header}
        <div className="qabar">
          <span className="muted sm">Guide ready — hand it to QA.</span>
          <span className="spacer" />
          {d.connected && (
            <button className="btn soft" type="button" onClick={gen}>
              Regenerate
            </button>
          )}
          <button className="btn primary" type="button" onClick={copy}>
            {copied ? "Copied ✓" : "Copy guide"}
          </button>
        </div>
        <div className="card">
          <Md className="qaguide">{d.md}</Md>
        </div>
      </>
    );
  }

  // failed / stopped / none
  const note =
    d.state === "stopped" ? (
      <div className="banner warn">
        <span>🛑</span>
        <div>
          <b>Stopped.</b> Generate a new guide below.
        </div>
      </div>
    ) : d.failed ? (
      <div className="banner err">
        <span>🔴</span>
        <div>{d.failed}</div>
      </div>
    ) : null;

  return (
    <>
      {header}
      {note}
      <div className="card top">
        {d.state === "none" && !note && <h4 style={{ marginTop: 0 }}>No guide yet</h4>}
        {d.state === "none" && !note && (
          <p className="muted sm">
            Build a tester-ready QA guide from this PR's diff, review threads and history.
          </p>
        )}
        {d.connected ? (
          <button className="btn primary" type="button" onClick={gen}>
            Generate QA guide
          </button>
        ) : (
          gate
        )}
      </div>
    </>
  );
}

export function Qa() {
  const { search } = useLocation();
  const pr = search.get("pr") || "";
  return pr ? <QaDetailView pr={pr} /> : <QaIndex />;
}
