import { useEffect, useMemo, useState } from "react";
import { api, type Me, type QueueData, type QueueRow } from "./api";
import { Link, useLocation } from "./router";

const SORTS: [string, string][] = [
  ["newest", "Newest"],
  ["oldest", "Oldest"],
  ["activity", "Recent activity"],
  ["findings", "Most findings"],
];

const EMPTY: Record<string, [string, string, string]> = {
  todo: ["🎉", "You're all caught up", "No PRs are waiting on your review."],
  reviewed: ["📝", "Nothing to post", "Reviews you've run and not yet posted show here."],
  posted: ["💬", "Nothing pending approval", "PRs you've commented on but not approved."],
  approved: ["✅", "Nothing approved yet", "PRs you approve will be listed here."],
  archived: ["🗂️", "No archived PRs", "Archived PRs are hidden from your working set."],
};


function Row({ row }: { row: QueueRow }) {
  return (
    <div className="row">
      <Link className="rowlink" to={`/pr?pr=${row.num}`}>
        <div className="rowtop">
          <span className="num">#{row.num}</span>
          <span className="ttl">{row.title}</span>
        </div>
        <div className="muted sm rowsub">
          {row.author && <span>{row.author}</span>}
          {row.size && <span>{row.size}</span>}
          {row.when.map((w, i) => (
            <span key={i}>{w}</span>
          ))}
          {row.sev.length > 0 && (
            <span className="chipwrap">
              {row.sev.map((s) => (
                <span key={s.kind} className={"pill " + s.kind}>
                  {s.n} {s.label}
                </span>
              ))}
            </span>
          )}
        </div>
      </Link>
      <div className="rowmeta">
        <span className={"pill " + row.state}>{row.state}</span>
        <span className="rowact">{row.archived ? "restore" : "archive"}</span>
        <Link className="chev" to={`/pr?pr=${row.num}`} aria-hidden="true">
          ›
        </Link>
      </div>
    </div>
  );
}

export function Queue({ me }: { me: Me }) {
  const { search } = useLocation();
  const tab = search.get("tab") || "todo";
  const sort = search.get("sort") || "newest";
  const [data, setData] = useState<QueueData | null>(null);
  const [q, setQ] = useState("");

  useEffect(() => {
    let live = true;
    api.queue(tab, sort).then((d) => live && setData(d));
    return () => {
      live = false;
    };
  }, [tab, sort]);

  const filtered = useMemo(() => {
    if (!data) return [];
    const needle = q.trim().toLowerCase();
    if (!needle) return data.rows;
    return data.rows.filter((r) =>
      `#${r.num} ${r.title} ${r.author}`.toLowerCase().includes(needle)
    );
  }, [data, q]);

  if (!data) return <div className="wrap-load muted">Loading…</div>;
  const empty = EMPTY[tab] || ["📭", "Nothing here yet", "This view is empty."];

  return (
    <>
      <h1>Your review queue</h1>
      <p className="muted sm">
        Reviews requested from you across <code>{me.repo}</code>. Nothing reaches GitHub without
        your click.
      </p>
      {!data.slackOk && (
        <div className="banner warn">
          <span>💬</span>
          <div>
            No Slack member ID yet — review requests won't ping you.{" "}
            <Link to="/integrations">Add it in Integrations.</Link>
          </div>
        </div>
      )}

      <div className="stats">
        {(["todo", "reviewed", "posted", "approved"] as const).map((k, i) => (
          <Link
            key={k}
            className={"stat" + (i === 0 ? " hot" : "") + (tab === k ? " on" : "")}
            to={`/?tab=${k}&sort=${sort}`}
          >
            <div className="k">{data.stats[k]}</div>
            <div className="l">
              {k === "todo"
                ? "Awaiting your review"
                : k === "reviewed"
                ? "Ready to post"
                : k === "posted"
                ? "Pending approval"
                : "Approved"}
            </div>
          </Link>
        ))}
      </div>

      <div className="tabs">
        {data.tabs.map((t) => (
          <Link key={t.key} className={"tab" + (tab === t.key ? " on" : "")} to={`/?tab=${t.key}&sort=${sort}`}>
            {t.label}
            <span className="cnt">{t.count}</span>
          </Link>
        ))}
      </div>
      <div className="tabdesc">{data.tabDesc}</div>

      <div className="qtools">
        <input
          id="qsearch"
          className="in"
          type="search"
          autoComplete="off"
          placeholder="Filter your queue…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <div className="sortbar">
          <span className="muted sm">Sort</span>
          {SORTS.map(([k, lbl]) => (
            <Link key={k} className={"sortopt" + (sort === k ? " on" : "")} to={`/?tab=${tab}&sort=${k}`}>
              {lbl}
            </Link>
          ))}
        </div>
      </div>


      {filtered.length > 0 ? (
        <div className="list" id="qlist" data-tour="queuelist">
          {filtered.map((r) => (
            <Row key={r.num} row={r} />
          ))}
        </div>
      ) : q ? (
        <div className="empty">
          <span className="ic">🔍</span>
          <b>No matches</b>
          Nothing in this view matches your search.
        </div>
      ) : (
        <div className="empty">
          <span className="ic">{empty[0]}</span>
          <b>{empty[1]}</b>
          {empty[2]}
        </div>
      )}
    </>
  );
}
