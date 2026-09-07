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
  label: string;
  sub?: string;
  run: () => void;
}

const SECTIONS: [string, string][] = [
  ["Queue", "/"],
  ["QA guides", "/qa"],
  ["Learnings", "/learnings"],
  ["Skills", "/skills"],
  ["Integrations", "/integrations"],
  ["How it works", "/how"],
];

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [rows, setRows] = useState<QueueRow[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

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

  // ⌘K / Ctrl-K toggles the palette from anywhere; the event opens it.
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

  // On open: focus the input and fetch the queue once for the "Your PRs" list (best-effort).
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
    // rows deliberately excluded — fetch at most once per session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const num = prnum(q);
  const needle = q.trim().toLowerCase();

  const cmds: Cmd[] = useMemo(() => {
    const out: Cmd[] = [];
    if (num) {
      out.push({
        id: "review",
        label: `Review PR #${num}`,
        sub: "open the review page",
        run: () => go(`/pr?pr=${num}`),
      });
    }
    for (const r of rows) {
      if (needle && !`#${r.num} ${r.title} ${r.author}`.toLowerCase().includes(needle)) continue;
      if (out.filter((c) => c.id.startsWith("pr-")).length >= 6) break;
      out.push({ id: `pr-${r.num}`, label: `#${r.num} ${r.title}`, sub: r.author, run: () => go(`/pr?pr=${r.num}`) });
    }
    for (const [label, to] of SECTIONS) {
      if (needle && !label.toLowerCase().includes(needle)) continue;
      out.push({ id: `go-${to}`, label: `Go to ${label}`, run: () => go(to) });
    }
    return out;
  }, [num, needle, rows, go]);

  // Keep the selection in range as results change.
  useEffect(() => {
    setSel((s) => Math.max(0, Math.min(s, cmds.length - 1)));
  }, [cmds.length]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      close();
    } else if (e.key === "ArrowDown") {
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

  return (
    <div className="cmdk-back" onMouseDown={close}>
      <div className="cmdk" role="dialog" aria-label="Command palette" onMouseDown={(e) => e.stopPropagation()}>
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
        <div className="cmdk-list">
          {cmds.length === 0 ? (
            <div className="cmdk-empty">No matches — paste a PR number or URL to review it.</div>
          ) : (
            cmds.map((c, i) => (
              <button
                key={c.id}
                type="button"
                className={"cmdk-row" + (i === sel ? " sel" : "")}
                onMouseEnter={() => setSel(i)}
                onClick={c.run}
              >
                <span className="cmdk-label">{c.label}</span>
                {c.sub && <span className="cmdk-sub">{c.sub}</span>}
              </button>
            ))
          )}
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
