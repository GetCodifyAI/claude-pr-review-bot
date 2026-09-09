import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type Finding,
  type Me,
  type PrData,
  type ReviewersData,
  type RunFormData,
  type Token,
} from "./api";
import { Md } from "./Md";
import { MdEditor } from "./MdEditor";
import { Link, useLocation } from "./router";

const REV_STATE: Record<string, [string, string, string]> = {
  APPROVED: ["ok", "✓", "Approved"],
  CHANGES_REQUESTED: ["chg", "±", "Changes requested"],
  COMMENTED: ["cmt", "💬", "Commented"],
  DISMISSED: ["cmt", "○", "Dismissed"],
  AWAITING: ["await", "●", "Awaiting review"],
};

function Banner({ html }: { html: string }) {
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}

function Reviewers({ data }: { data: ReviewersData }) {
  if (!data.reviewers.length) return null;
  const dec =
    data.decision === "CHANGES_REQUESTED"
      ? "Changes requested must be addressed to merge."
      : data.decision === "APPROVED"
      ? "✅ Approved — ready to merge."
      : data.decision === "REVIEW_REQUIRED"
      ? "Review required before merge."
      : "";
  return (
    <details className="revcard">
      <summary>Reviewers ({data.reviewers.length})</summary>
      <div className="dbody">
        {data.reviewers.map((r) => {
          const [cls, ic, lbl] = REV_STATE[r.state] || ["await", "●", "Pending"];
          return (
            <div className="revrow" key={r.login}>
              <span className="revav">{(r.login[0] || "?").toUpperCase()}</span>
              <div className="revmeta">
                <span className="revname" title={r.login}>{r.login}</span>
                <span className={"revst " + cls}>
                  {ic} {lbl}
                </span>
              </div>
            </div>
          );
        })}
        {dec && <div className="hint" style={{ marginTop: 8 }}>{dec}</div>}
      </div>
    </details>
  );
}

function ClaudeGate({ action }: { action: string }) {
  return (
    <div className="claudegate">
      <div className="cg-ico">✳</div>
      <div className="cg-body">
        <b>Connect your Claude account to {action}</b>
        <p className="muted sm">
          {action[0].toUpperCase() + action.slice(1)}s run on <b>your own</b> Claude subscription —
          nothing runs on anyone else's plan. Connect once and you're set.
        </p>
        <Link className="btn primary" to="/integrations">
          Connect Claude →
        </Link>
      </div>
    </div>
  );
}

function RunForm({
  pr,
  token,
  form,
  label,
  connected,
  onStarted,
}: {
  pr: string;
  token: Token;
  form: RunFormData;
  label: string;
  connected: boolean;
  onStarted: () => void;
}) {
  const [effort, setEffort] = useState(form.suggested);
  const [model, setModel] = useState(form.suggested === "deep" ? "opus" : "");
  const [focus, setFocus] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const focusRef = useRef<HTMLTextAreaElement>(null);
  const others = form.othersOnHead || [];
  if (!connected) return <ClaudeGate action="review" />;
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        setErr("");
        setBusy(true);
        const r = await api.review(pr, token, effort, focus, model);
        if (r.started === false) {
          // A previous run still holds the per-PR lock (e.g. a stop that could not be confirmed).
          setBusy(false);
          setErr(
            "Couldn't start — a previous run may still be finishing or holding the lock. " +
              "Try Stop, then start again in a moment.",
          );
          return;
        }
        onStarted();
      }}
    >
      {err && (
        <div className="banner warn">
          <span>⚠️</span>
          <div>{err}</div>
        </div>
      )}
      {others.length > 0 && (
        <div className="nudge">
          <div className="nudge-h">
            {others.length === 1
              ? `${others[0].login} already reviewed this commit`
              : `${others.length} reviewers already reviewed this commit`}
          </div>
          <ul className="nudge-list">
            {others.map((o) => (
              <li key={o.login}>
                <b>{o.login}</b> — {o.effort}
                {o.model ? ` · ${o.model}` : ""} · {o.skill}
                {o.focus ? ` · focus: “${o.focus}”` : " · no focus"}
                {o.when ? ` · ${o.when}` : ""}
              </li>
            ))}
          </ul>
          <div className="nudge-cta">
            A second review adds the most when it checks something the first didn’t — add a focus
            below, or switch skills on the Skills page. Or run the same way to compare notes.{" "}
            <button type="button" className="linkbtn" onClick={() => focusRef.current?.focus()}>
              Add a focus
            </button>
          </div>
        </div>
      )}
      <div className="effort-lbl">Effort</div>
      <div className="effrow">
        {form.levels.map((l) => (
          <label key={l.key} className={"eff" + (effort === l.key ? " hot" : "")}>
            <input type="radio" name="effort" checked={effort === l.key} onChange={() => setEffort(l.key)} />
            <span className="effname">
              {l.name}
              {l.key === form.suggested ? " · suggested" : ""}
            </span>
            <span className="effsub">{l.sub}</span>
          </label>
        ))}
      </div>
      {form.models && form.models.length > 0 && (
        <>
          <div className="effort-lbl">Model</div>
          <div className="effrow">
            {form.models.map((m) => (
              <label key={m.key} className={"eff" + (model === m.key ? " hot" : "")}>
                <input
                  type="radio"
                  name="model"
                  checked={model === m.key}
                  onChange={() => setModel(m.key)}
                />
                <span className="effname">{m.name}</span>
                <span className="effsub">{m.sub}</span>
              </label>
            ))}
          </div>
        </>
      )}
      <div className="focuswrap">
        <div className="effort-lbl">Focus — optional</div>
        <textarea
          ref={focusRef}
          className="in"
          rows={2}
          value={focus}
          onChange={(e) => setFocus(e.target.value)}
          placeholder="Anything specific to check? e.g. “pay close attention to the order-flow cost calculation.”"
        />
      </div>
      <div className="runrow">
        <span className="hint" style={{ flex: 1 }}>
          Runs with {form.skillLabel} · deeper reviews cost more of your weekly usage.
        </span>
        <button className="btn primary" type="submit" disabled={busy}>
          {busy ? "Starting…" : label}
        </button>
      </div>
    </form>
  );
}

function HistoryList({ pr, runs }: { pr: string; runs: PrData["history"] }) {
  if (!runs || !runs.length) return null;
  return (
    <div className="card">
      <div className="effort-lbl">Earlier runs ({runs.length})</div>
      <div className="histlist">
        {runs.map((h) => (
          <Link key={h.ts} className="histrow" to={`/pr?pr=${pr}&v=${h.ts}`}>
            <span className="histwhen">earlier run</span>
            <span className="muted sm">
              {h.effort} · {h.findings} finding(s)
              {h.focus ? ` · focus: “${h.focus.slice(0, 80)}”` : ""}
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}

function ProgressPanel({ pr, data, onStop }: { pr: string; data: PrData; onStop: () => void }) {
  const r = data.reviewing!;
  const [stopping, setStopping] = useState(false);
  return (
    <div className="card top">
      <div className="prog-hd">
        Drafting review for <b>#{pr}</b> · <span className="muted sm">{r.effortLabel} effort</span>
      </div>
      <ul className="prog">
        {r.phases.map((ph, j) => (
          <li key={ph} className={j < r.cur ? "done" : j === r.cur ? "now" : ""}>
            <span className={"pm" + (j === r.cur ? " spin" : "")}>{j < r.cur ? "✓" : j === r.cur ? "" : "○"}</span>
            {ph}
          </li>
        ))}
      </ul>
      <div className="progbar">
        <div className="progfill" />
      </div>
      {r.focus && <div className="hint">🎯 Focusing on: “{r.focus}”</div>}
      {r.queued && (
        <div className="hint">Waiting for another review to finish first — one runs at a time on this box.</div>
      )}
      <div className="hint" style={{ marginTop: 10 }}>
        This page refreshes itself; {r.effortHint}.
      </div>
      <form
        style={{ marginTop: 12 }}
        onSubmit={async (e) => {
          e.preventDefault();
          setStopping(true);
          await api.stop(pr, data.tokens.stop);
          onStop();
        }}
      >
        <button className="btn soft" type="submit" disabled={stopping}>
          {stopping ? "Stopping…" : "Stop review"}
        </button>
      </form>
    </div>
  );
}

function FindingCard({
  f,
  checked,
  onToggle,
  body,
  onBody,
}: {
  f: Finding;
  checked: boolean;
  onToggle: () => void;
  body: string;
  onBody: (v: string) => void;
}) {
  return (
    <div className="finding">
      <div className="fhead">
        <input type="checkbox" className="fsel" checked={checked} onChange={onToggle} />
        <label>
          <span className={"pill " + f.severity}>{f.sevLabel}</span>
        </label>
        {f.agreement?.confirmed ? (
          <span className="agree ok" title={`Also raised by ${f.agreement.by.join(", ")}`}>
            ✓ Confirmed by {f.agreement.n} independent reviews ({f.agreement.differ})
          </span>
        ) : (
          f.agreement && (
            <span className="agree solo">Only flagged by your run</span>
          )
        )}
        <span className="loc">
          {f.path}:{f.line}
        </span>
        <span className="thread">{f.thread ? `↩ reply to ${f.thread}` : "new thread"}</span>
      </div>
      {f.title && <div className="ftitle">{f.title}</div>}
      {f.impact && (
        <div className="fimpact">
          <span className="fimpact-l">Why it matters</span> {f.impact}
        </div>
      )}
      <details className="fdetail">
        <summary>Details &amp; edit — this is the comment posted to GitHub</summary>
      <div className="fbody">
        <MdEditor value={body} onChange={onBody} />
        {f.suggestion && (
          <div className="sugg">
            <div className="sugglabel">💡 Suggested change — the author can apply this in one click on GitHub</div>
            <pre className="suggin-pre">
              <code>{f.suggestion}</code>
            </pre>
          </div>
        )}
      </div>
      </details>
    </div>
  );
}

function ReviewBody({ data, onDone }: { data: PrData; onDone: () => void }) {
  const rev = data.review!;
  const [bodies, setBodies] = useState<Record<number, string>>(
    () => Object.fromEntries(rev.findings.map((f) => [f.i, f.body]))
  );
  const [selected, setSelected] = useState<Set<number>>(
    () => new Set(rev.findings.filter((f) => !f.low).map((f) => f.i))
  );
  const [requestChanges, setRequestChanges] = useState(false);
  const [banner, setBanner] = useState("");
  const [busy, setBusy] = useState(false);

  // approve
  const [approveBody, setApproveBody] = useState(rev.approve?.defaultMsg || "");
  const [ack, setAck] = useState(false);

  const toggle = (i: number) =>
    setSelected((s) => {
      const n = new Set(s);
      n.has(i) ? n.delete(i) : n.add(i);
      return n;
    });

  const shown = rev.findings.filter((f) => !f.low);
  const maybe = rev.findings.filter((f) => f.low);

  async function submitPost(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const res = await api.post(data.pr, data.tokens.post, {
      selected: [...selected],
      bodies,
      suggs: {},
      request_changes: requestChanges,
    });
    setBanner(res.bannerHtml);
    setBusy(false);
    onDone();
  }

  async function submitApprove(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const res = await api.approve(data.pr, data.tokens.approve, approveBody, ack);
    setBanner(res.bannerHtml);
    setBusy(false);
    onDone();
  }

  const renderFinding = (f: Finding) => (
    <FindingCard
      key={f.i}
      f={f}
      checked={selected.has(f.i)}
      onToggle={() => toggle(f.i)}
      body={bodies[f.i] ?? ""}
      onBody={(v) => setBodies((b) => ({ ...b, [f.i]: v }))}
    />
  );

  return (
    <>
      {data.dryRun && (
        <div className="banner warn">
          <span>🧪</span>
          <div>
            <b>DRY RUN — the buttons on this page do not write to GitHub.</b>
          </div>
        </div>
      )}
      {banner && <Banner html={banner} />}

      <h2>Assessment</h2>
      {rev.reused && (
        <div className="banner ok">
          <span>♻️</span>
          <div>Reused your earlier run of this exact configuration on this commit — 0 new tokens.</div>
        </div>
      )}
      <div className="card">
        <div className="meta">
          <span className={"pill " + (rev.event === "REQUEST_CHANGES" ? "blocker" : "posted")}>{rev.event}</span>
          <span className="muted sm">the agent's read — comments post as a plain review either way</span>
        </div>
        <Md>{rev.summary}</Md>
        {rev.chips.length > 0 && (
          <div className="chips">
            {rev.chips.map((c) => (
              <span key={c.kind} className={"pill " + c.kind}>
                {c.n} {c.label}
              </span>
            ))}
          </div>
        )}
      </div>

      {rev.explainer && (
        <details>
          <summary>What this PR does</summary>
          <Md className="dbody">{rev.explainer}</Md>
        </details>
      )}
      {rev.analysis && (
        <details>
          <summary>Analysis — what I checked, and what I dropped</summary>
          <Md className="dbody">{rev.analysis}</Md>
        </details>
      )}

      <h2>Findings ({rev.count})</h2>
      {rev.convergence && rev.convergence.total > 0 && (
        <div className="conv-summary">
          <b>{rev.convergence.confirmed}</b> of {rev.convergence.total} finding(s) confirmed by
          independent reviews ({rev.convergence.rate}% agreement across {rev.convergence.nRuns}{" "}
          reviewers on this commit). A finding only counts as confirmed when a reviewer using a
          different skill/model/effort raised it too — a signal to build on, not a score.
        </div>
      )}
      {rev.count === 0 ? (
        <div className="card">
          <p className="muted">No findings — nothing to post.</p>
        </div>
      ) : (
        <form onSubmit={submitPost}>
          {rev.posted && (
            <div className="banner ok">
              <span>✓</span>
              <div>
                Posted to GitHub as your review.{" "}
                <a href={data.ghUrl} target="_blank" rel="noreferrer">View on GitHub</a>.
              </div>
            </div>
          )}
          {shown.map(renderFinding)}
          {maybe.length > 0 && (
            <details className="maybe">
              <summary>
                🤔 Maybe — {maybe.length} lower-confidence finding{maybe.length !== 1 ? "s" : ""} (unchecked)
              </summary>
              <div className="dbody">{maybe.map(renderFinding)}</div>
            </details>
          )}
          {!rev.posted && (
            <div className="bar">
              <div className="inner">
                <span className="muted sm">
                  <b>{selected.size}</b> selected ·{" "}
                  {requestChanges ? "requests changes — can block the PR until updated" : "posts as plain comments"}
                </span>
                <span className="spacer" />
                <label className="rqtoggle">
                  <input type="checkbox" checked={requestChanges} onChange={(e) => setRequestChanges(e.target.checked)} />{" "}
                  Request changes instead
                </label>
                <button className={"btn " + (requestChanges ? "warn" : "primary")} type="submit" disabled={busy}>
                  {requestChanges ? "Request changes" : rev.postLabel}
                </button>
              </div>
            </div>
          )}
        </form>
      )}

      {rev.approved ? (
        <ApprovedCard a={rev.approved} ghUrl={data.ghUrl} />
      ) : (
        rev.approve && (
          <>
            <h2>Approve</h2>
            <div className="card">
              {rev.approve.lgtm ? (
                <div className="banner ok">
                  <span>✅</span>
                  <div>
                    <b>LGTM</b> — no blockers.
                  </div>
                </div>
              ) : (
                <div className="banner warn">
                  <span>⚠️</span>
                  <div>
                    <b>Not LGTM</b> —{" "}
                    {rev.approve.blockers ? `${rev.approve.blockers} blocker(s)` : "the agent's assessment is REQUEST_CHANGES"}
                    . Approving anyway needs the confirmation below.
                  </div>
                </div>
              )}
              <form onSubmit={submitApprove}>
                <label className="muted sm">
                  Approval comment — posted on the PR as a whole, then the PR is approved
                </label>
                <MdEditor value={approveBody} onChange={setApproveBody} />
                {!rev.approve.lgtm && (
                  <p className="sm">
                    <label>
                      <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} required /> I've
                      read the findings above and want to approve anyway.
                    </label>
                  </p>
                )}
                <p>
                  <button className="btn primary" type="submit" disabled={busy}>
                    {data.dryRun ? "Approve (dry run)" : `Approve #${data.pr}`}
                  </button>
                </p>
              </form>
            </div>
          </>
        )
      )}

      <RerunSection data={data} onDone={onDone} />
    </>
  );
}

function ApprovedCard({ a, ghUrl }: { a: NonNullable<PrData["approved"]>; ghUrl: string }) {
  return (
    <>
      <h2>Approved</h2>
      <div className="card">
        <div className="banner ok">
          <span>✅</span>
          <div>
            <b>
              {a.manual ? "Marked as approved" : "Approved"} on {a.at}
            </b>{" "}
            ({a.ago}){!a.manual && <> as <code>{a.user}</code></>}
          </div>
        </div>
        {a.body && !a.manual && (
          <>
            <p className="muted sm">Comment posted with the approval:</p>
            <pre>
              <code>{a.body}</code>
            </pre>
          </>
        )}
        <p>
          <a className="btn" href={ghUrl} target="_blank" rel="noopener">
            View on GitHub
          </a>
        </p>
      </div>
    </>
  );
}

function RerunSection({ data, onDone }: { data: PrData; onDone: () => void }) {
  return (
    <div id="rerun">
      <h2>Re-run</h2>
      <div className="card">
        <p className="muted sm" style={{ marginTop: 0 }}>
          Run it again — a fresh effort level or a focus note. The current review is kept in history below.
        </p>
        <RunForm
          pr={data.pr}
          token={data.tokens.review}
          form={data.runForm}
          label="Re-run review"
          connected={data.claudeConnected}
          onStarted={onDone}
        />
        <HistoryList pr={data.pr} runs={data.history} />
      </div>
    </div>
  );
}

function usageChip(u: NonNullable<PrData["usage"]>): string {
  return `${u.model.replace(/^claude-/, "")} · ${u.realTokens.toLocaleString()} tokens`;
}

// Details on hover: the real breakdown, plus the API-list-price estimate clearly marked as NOT
// what a Claude subscription is billed (it isn't per-token).
function usageTitle(u: NonNullable<PrData["usage"]>): string {
  const cache = u.cacheReadTokens + u.cacheCreationTokens;
  const cost =
    u.costUsd > 0
      ? ` · ≈ $${u.costUsd.toLocaleString(undefined, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })} at API list prices (not billed on your Claude subscription)`
      : "";
  return (
    `${u.inputTokens.toLocaleString()} input · ${u.outputTokens.toLocaleString()} output · ` +
    `${cache.toLocaleString()} cached context re-reads${cost}`
  );
}

function HeaderTop({ data }: { data: PrData }) {
  return (
    <>
      <nav className="bc">
        <Link to="/">Queue</Link>
        <span className="sep">/</span>
        <span className="cur">#{data.pr}</span>
      </nav>
      <h1 className="prtitle">
        #{data.pr} — {data.title}
      </h1>
    </>
  );
}

// The GitHub-style right rail: quick actions, PR details, and review progress, grouped.
function PrSidebar({ data }: { data: PrData }) {
  return (
    <div className="prside-inner">
      <div className="sidecard">
        <div className="sidehead">Actions</div>
        <a className="sideact" href={data.ghUrl} target="_blank" rel="noopener">
          <span className="sideact-ico">↗</span> Open on GitHub
        </a>
        <Link className="sideact" to={`/qa?pr=${data.pr}`}>
          <span className="sideact-ico">🧪</span> QA guide
        </Link>
        <Link className="sideact" to={`/stack?pr=${data.pr}`}>
          <span className="sideact-ico">🔗</span> Stacked review
        </Link>
      </div>

      <div className="sidecard">
        <div className="sidehead">Details</div>
        <div className="siderow">
          <span className={"pill " + data.state}>{data.state}</span>
          {data.dryRun && <span className="pill dry">dry run</span>}
          {data.effortBadge && (
            <span className="effbadge" title={data.effortBadge.hint}>
              {data.effortBadge.label}
            </span>
          )}
        </div>
        {data.usage && (
          <div className="sideusage" title={usageTitle(data.usage)}>
            {usageChip(data.usage)}
          </div>
        )}
        {(data.author || data.size) && (
          <div className="sidemeta">
            {data.author}
            {data.size ? ` · ${data.size}` : ""}
          </div>
        )}
        {data.runner && (
          <div className="sidemeta muted">
            Ran on {data.runner !== "shared" ? `${data.runner}'s` : "the shared team"} Claude account
          </div>
        )}
        {!data.awaiting && <div className="sidemeta muted">Not awaiting your review</div>}
      </div>

      <div className="sidecard">
        <div className="sidehead">Review progress</div>
        <div className="sidesteps">
          {data.timeline.map((s) => (
            <div key={s.label} className={"sidestep" + (s.done ? " hit" : "")}>
              <span className="sidetick">{s.done ? "✓" : "○"}</span>
              <span>
                {s.label}
                {s.note && <span className="muted"> {s.note}</span>}
              </span>
            </div>
          ))}
        </div>
        {data.reviewers && <Reviewers data={data.reviewers} />}
      </div>
    </div>
  );
}

// Contextual alert banners (risk / focus / stale) — shown atop the main column.
function PrBanners({ data }: { data: PrData }) {
  return (
    <>
      {data.risk.map((r) => (
        <div className="banner info" key={r.title}>
          <span>{r.icon}</span>
          <div>
            <b>{r.title}.</b> {r.note}
          </div>
        </div>
      ))}
      {data.focus && data.state === "done" && (
        <div className="banner info">
          <span>🎯</span>
          <div>
            <b>Focused review.</b> You asked Robin to focus on: “{data.focus}”.
          </div>
        </div>
      )}
      {data.stale && (
        <div className="banner warn">
          <span>🔄</span>
          <div>
            <b>The author pushed new commits since this review.</b> The findings may be out of date —
            re-run below.
          </div>
        </div>
      )}
    </>
  );
}

export function PrPage(_props: { me: Me }) {
  const { search } = useLocation();
  const pr = search.get("pr") || "";
  const v = search.get("v") || "";
  const [data, setData] = useState<PrData | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const load = useCallback(() => {
    if (!pr) return;
    api.pr(pr, v || undefined).then(setData);
  }, [pr, v]);

  useEffect(() => {
    setData(null);
    load();
  }, [load]);

  // Auto-refresh while a review is in progress.
  useEffect(() => {
    window.clearInterval(timer.current);
    if (data && (data.state === "reviewing" || data.state === "queued")) {
      timer.current = window.setInterval(load, 4000);
    }
    return () => window.clearInterval(timer.current);
  }, [data, load]);

  if (!data) return <div className="muted">Loading…</div>;

  if (data.historyView) {
    return (
      <>
        <nav className="bc">
          <Link to="/">Queue</Link>
          <span className="sep">/</span>
          <Link to={`/pr?pr=${data.pr}`}>#{data.pr}</Link>
          <span className="sep">/</span>
          <span className="cur">earlier run</span>
        </nav>
        <h1 className="prtitle">
          #{data.pr} — {data.title}
        </h1>
        <div className="banner info">
          <span>🕓</span>
          <div>
            <b>Viewing an earlier run</b> from {data.when}. <Link to={`/pr?pr=${data.pr}`}>Back to the current review</Link>.
          </div>
        </div>
        <h2>Assessment</h2>
        <div className="card">
          <Md>{data.summary || ""}</Md>
        </div>
        <h2>Findings ({data.findings?.length || 0})</h2>
        {(data.findings || []).map((f, i) => (
          <div className="card" key={i}>
            <div className="meta">
              <span className={"pill " + f.severity}>{f.sevLabel}</span>
              <code>
                {f.path}:{f.line}
              </code>
            </div>
            <Md>{f.body}</Md>
          </div>
        ))}
      </>
    );
  }

  return (
    <>
      <HeaderTop data={data} />
      <div className="prlayout">
        <div className="prmain">
          <PrBanners data={data} />
          {data.reviewing && <ProgressPanel pr={pr} data={data} onStop={load} />}
      {data.stopped && (
        <>
          <div className="banner warn">
            <span>🛑</span>
            <div>
              <b>Review stopped.</b>{" "}
              {data.stopped.halted ? "✅ No agent is running — Claude usage has halted." : "⚠️ A process may still be running."}{" "}
              Start a new run below.
            </div>
          </div>
          <div className="card top">
            <RunForm
              pr={pr}
              token={data.tokens.review}
              form={data.runForm}
              label="Start review"
              connected={data.claudeConnected}
              onStarted={load}
            />
          </div>
          <HistoryList pr={pr} runs={data.history} />
        </>
      )}
      {data.stalled && (
        <>
          <div className="banner err">
            <span>🔴</span>
            <div>
              <b>The review stopped before it finished.</b> It was at <code>{data.stalled.was}</code>. Re-run below.
            </div>
          </div>
          <div className="card top">
            <RunForm
              pr={pr}
              token={data.tokens.review}
              form={data.runForm}
              label="Re-run review"
              connected={data.claudeConnected}
              onStarted={load}
            />
          </div>
          <HistoryList pr={pr} runs={data.history} />
        </>
      )}
      {data.notReviewed && !data.approved && (
        <>
          {data.failed && (
            <div className="banner err">
              <span>🔴</span>
              <div>{data.failed}</div>
            </div>
          )}
          <div className="card top">
            <h4>Not reviewed here</h4>
            <p className="muted sm">No review has been run for this PR on this box.</p>
            <RunForm
              pr={pr}
              token={data.tokens.review}
              form={data.runForm}
              label="Run review"
              connected={data.claudeConnected}
              onStarted={load}
            />
          </div>
          <HistoryList pr={pr} runs={data.history} />
        </>
      )}
      {data.notReviewed && data.approved && <ApprovedCard a={data.approved} ghUrl={data.ghUrl} />}
          {data.review && <ReviewBody data={data} onDone={load} />}
        </div>
        <aside className="prside">
          <PrSidebar data={data} />
        </aside>
      </div>
    </>
  );
}
