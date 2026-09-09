import { useEffect, useRef, useState } from "react";
import type { Me } from "./api";
import { openPalette } from "./CommandPalette";
import { NavIcon } from "./icons";
import { Link, useLocation } from "./router";
import { startTour } from "./Tour";

// Two groups: WORK (what you do day to day) and SETUP (configure once). "How it works" is no
// longer a primary nav item — it lives behind the footer Help menu.
const GROUPS: { label: string; items: [string, string, string][] }[] = [
  {
    label: "Work",
    items: [
      ["queue", "Queue", "/"],
      ["qa", "QA guides", "/qa"],
      ["learnings", "Learnings", "/learnings"],
      ["dashboard", "Insights", "/dashboard"],
    ],
  },
  {
    label: "Setup",
    items: [
      ["skills", "Skills", "/skills"],
      ["integrations", "Integrations", "/integrations"],
    ],
  },
];

function activeKey(path: string): string {
  if (path === "/") return "queue";
  if (path.startsWith("/qa")) return "qa";
  if (path.startsWith("/learnings")) return "learnings";
  if (path.startsWith("/dashboard")) return "dashboard";
  if (path.startsWith("/skills")) return "skills";
  if (path.startsWith("/integrations") || path.startsWith("/settings")) return "integrations";
  if (path.startsWith("/pr") || path.startsWith("/stack")) return "queue";
  return "";
}

function HelpMenu() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);
  return (
    <div className="helpwrap" ref={ref}>
      <button className="so" type="button" onClick={() => setOpen((o) => !o)}>
        ? Help
      </button>
      {open && (
        <div className="helpmenu" role="menu">
          <Link className="helpitem" to="/how" onClick={() => setOpen(false)}>
            How it works
          </Link>
          <button
            className="helpitem"
            type="button"
            onClick={() => {
              setOpen(false);
              startTour();
            }}
          >
            Take a tour
          </button>
        </div>
      )}
    </div>
  );
}

export function Sidebar({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  const { path } = useLocation();
  const active = activeKey(path);
  const skill = me.active_skill === "own" ? "your skill" : "team default";
  return (
    <aside className="side">
      <Link className="brand" to="/">
        {me.logo && <img src={me.logo} alt="" />}
        <span className="n">{me.brand}</span>
      </Link>

      <button className="reviewbtn" type="button" onClick={openPalette}>
        <span className="rb-ico">✨</span>
        <span className="rb-label">Review a PR</span>
        <kbd className="rb-kbd">⌘K</kbd>
      </button>

      <nav className="nav">
        {GROUPS.map((g) => (
          <div className="navgroup" key={g.label}>
            <div className="navlabel">{g.label}</div>
            {g.items.map(([k, label, to]) => (
              <Link key={k} to={to} className={"ni" + (active === k ? " on" : "")} data-tour={k}>
                {NavIcon[k]}
                <span>{label}</span>
              </Link>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidefoot">
        <div className="acct">
          <span className="av">{(me.login || "?").slice(0, 1).toUpperCase()}</span>
          <div className="whot">
            <span className="nm">{me.login}</span>
            <Link className="skillline" to="/skills" title="Which skill runs your reviews">
              ⚙ {skill}
            </Link>
          </div>
          <span
            className={"statuspill " + (me.dry_run ? "dry" : "on")}
            title={me.dry_run ? "Dry run — nothing posts to GitHub" : "Live"}
          >
            {me.dry_run ? "Dry" : "Live"}
          </span>
        </div>
        <div className="foota">
          <HelpMenu />
          <span className="spacer" />
          <button className="so" type="button" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </div>
    </aside>
  );
}
