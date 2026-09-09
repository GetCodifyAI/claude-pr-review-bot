// Tiny hand-rolled router (no react-router — matches Mynt). Robin serves at the site root;
// it still also answers under the legacy /prbot prefix, so we strip a leading /prbot when
// reading the URL (old bookmarks/Slack links) and always build clean root-relative links.

import { useEffect, useState } from "react";

const LEGACY = /^\/prbot(?=\/|$)/;

export function toPath(loc: string): string {
  let p = loc.replace(LEGACY, "") || "/";
  if (p.length > 1) p = p.replace(/\/$/, "");
  return p || "/";
}

export function navigate(to: string): void {
  const url = to.replace(LEGACY, "") || "/";
  window.history.pushState({}, "", url);
  window.dispatchEvent(new Event("robin:navigate"));
}

export function useLocation(): { path: string; search: URLSearchParams } {
  const read = () => ({
    path: toPath(window.location.pathname),
    search: new URLSearchParams(window.location.search),
  });
  const [loc, setLoc] = useState(read);
  useEffect(() => {
    const on = () => setLoc(read());
    window.addEventListener("popstate", on);
    window.addEventListener("robin:navigate", on);
    return () => {
      window.removeEventListener("popstate", on);
      window.removeEventListener("robin:navigate", on);
    };
  }, []);
  return loc;
}

export function Link(
  props: React.AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }
) {
  const { to, onClick, children, ...rest } = props;
  const href = to.startsWith("http") ? to : to.replace(LEGACY, "") || "/";
  const external = to.startsWith("http");
  return (
    <a
      href={href}
      onClick={(e) => {
        onClick?.(e);
        if (external || e.defaultPrevented || e.metaKey || e.ctrlKey) return;
        e.preventDefault();
        navigate(to);
      }}
      {...rest}
    >
      {children}
    </a>
  );
}
