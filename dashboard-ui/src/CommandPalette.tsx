import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type QueueRow } from "./api";
import { prnum } from "./pr";
import { navigate } from "./router";

// The command palette is the one place to search + review any PR. Opened by the sidebar's
// "Review a PR" button, by ⌘K / Ctrl-K anywhere, or by the robin:open-palette event.
const EVT = "robin:open-palette";
export function openPalette() {
  window.dispatchEvent(new Event(EVT));
}

interface Cmd {
  id: string;
  group: string;
  run: () => void;
  num?: string; // PR rows
  title?: string;
  author?: string;
  label?: string; // action / nav rows
  sub?: string;
  icon?: string;
}

const SECTIONS: [string, string, string][] = [
  ["Queue", "/", "◧"],
  ["QA guides", "/qa", "◑"],
  ["Learnings", "/learnings", "✦"],
  ["Skills", "/skills", "◇"],
  ["Integrations", "/integrations", "▦"],
  ["How it works", "/how", "?"],
];

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [rows, setRows] = useState<QueueRow[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    setQ("");
    setSel(0);
  }, []);
  const go = useCallback(
    (to: string) => {
      navigate(to);
      close();
    },
    [close],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    const onEvt = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener(EVT, onEvt);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(EVT, onEvt);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    setSel(0);
    inputRef.current?.focus();
    if (rows.length === 0) {
      api
        .queue("all", "newest")
        .then((d) => setRows(d.rows))
        .catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const num = prnum(q);
  const needle = q.trim().toLowerCase();

  const cmds: Cmd[] = useMemo(() => {
    const out: Cmd[] = [];
    if (num) {
      out.push({
        id: "review",
        group: "Review",
        label: `Review PR #${num}`,
        sub: "open the review page",
        icon: "✨",
        run: () => go(`/pr?pr=${num}`),
      });
    }
    let n = 0;
    for (const r of rows) {
      if (needle && !`#${r.num} ${r.title} ${r.author}`.toLowerCase().includes(needle)) continue;
      if (n++ >= 6) break;
      out.push({
        id: `pr-${r.num}`,
        group: "Your PRs",
        num: r.num,
        title: r.title,
        author: r.author,
        run: () => go(`/pr?pr=${r.num}`),
      });
    }
    for (const [label, to, icon] of SECTIONS) {
      if (needle && !label.toLowerCase().includes(needle)) continue;
      out.push({ id: `go-${to}`, group: "Go to", label, icon, run: () => go(to) });
    }
    return out;
  }, [num, needle, rows, go]);

  useEffect(() => {
    setSel((s) => Math.max(0, Math.min(s, cmds.length - 1)));
  }, [cmds.length]);

  // keep the highlighted row in view
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-i="${sel}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(s + 1, cmds.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(s - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      cmds[sel]?.run();
    }
  };

  let lastGroup = "";
  return (
    <div className="cmdk-back" onMouseDown={close}>
      <div className="cmdk" role="dialog" aria-label="Command palette" onMouseDown={(e) => e.stopPropagation()}>
        <div className="cmdk-inwrap">
          <span className="cmdk-search" aria-hidden="true">⌕</span>
          <input
            ref={inputRef}
            className="cmdk-in"
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="Paste a PR number or URL, or jump to…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKeyDown}
          />
        </div>
        <div className="cmdk-list" ref={listRef}>
          {cmds.length === 0 && (
            <div className="cmdk-empty">No matches — paste a PR number or URL to review it.</div>
          )}
          {cmds.map((c, i) => {
            const header = c.group !== lastGroup ? c.group : null;
            lastGroup = c.group;
            return (
              <div key={c.id}>
                {header && <div className="cmdk-grouphead">{header}</div>}
                <button
                  type="button"
                  data-i={i}
                  className={"cmdk-row" + (i === sel ? " sel" : "")}
                  onMouseEnter={() => setSel(i)}
                  onClick={c.run}
                >
                  {c.num ? (
                    <>
                      <span className="cmdk-num">#{c.num}</span>
                      <span className="cmdk-title">{c.title}</span>
                      {c.author && <span className="cmdk-sub">{c.author}</span>}
                    </>
                  ) : (
                    <>
                      {c.icon && <span className="cmdk-ico">{c.icon}</span>}
                      <span className="cmdk-title">{c.label}</span>
                      {c.sub && <span className="cmdk-sub">{c.sub}</span>}
                    </>
                  )}
                </button>
              </div>
            );
          })}
        </div>
        <div className="cmdk-foot">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd> navigate
          </span>
          <span>
            <kbd>↵</kbd> open
          </span>
          <span>
            <kbd>esc</kbd> close
          </span>
        </div>
      </div>
    </div>
  );
}
