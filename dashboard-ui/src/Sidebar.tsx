import type { Me } from "./api";
import { NavIcon } from "./icons";
import { Link, useLocation } from "./router";
import { startTour } from "./Tour";

const NAV: [string, string, string][] = [
  ["queue", "Queue", "/"],
  ["qa", "QA guide", "/qa"],
  ["learnings", "Learnings", "/learnings"],
  ["skills", "Skills", "/skills"],
  ["integrations", "Integrations", "/integrations"],
  ["how", "How it works", "/how"],
];

function activeKey(path: string): string {
  if (path === "/") return "queue";
  if (path.startsWith("/qa")) return "qa";
  if (path.startsWith("/learnings")) return "learnings";
  if (path.startsWith("/skills")) return "skills";
  if (path.startsWith("/integrations") || path.startsWith("/settings")) return "integrations";
  if (path.startsWith("/how")) return "how";
  if (path.startsWith("/pr") || path.startsWith("/stack")) return "queue";
  return "queue";
}

export function Sidebar({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  const { path } = useLocation();
  const active = activeKey(path);
  const skill = me.active_skill === "own" ? "your skill" : "team default";
  return (
    <aside className="side">
      <Link className="brand" to="/">
        <img src="https://github.com/favicon.ico" alt="" style={{ display: "none" }} />
        <span className="n">{me.brand}</span>
      </Link>
      <nav className="nav">
        {NAV.map(([k, label, to]) => (
          <Link key={k} to={to} className={"ni" + (active === k ? " on" : "")} data-tour={k}>
            {NavIcon[k]}
            <span>{label}</span>
          </Link>
        ))}
      </nav>
      <div className="sidefoot">
        <span className={"live " + (me.dry_run ? "dry" : "on")}>{me.dry_run ? "dry run" : "live"}</span>
        <div className="who">
          <span className="av">{(me.login || "?").slice(0, 1).toUpperCase()}</span>
          <div className="whot">
            <span className="nm">{me.login}</span>
            <Link className="skillline" to="/skills" title="Which skill runs your reviews">
              ⚙ {skill}
            </Link>
          </div>
        </div>
        <div className="foota">
          <a className="so" href="#" onClick={(e) => { e.preventDefault(); startTour(); }}>
            Take a tour
          </a>
          <a className="so" href="#" onClick={(e) => { e.preventDefault(); onSignOut(); }}>
            Sign out
          </a>
        </div>
      </div>
    </aside>
  );
}
