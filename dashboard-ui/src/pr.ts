// Parse a PR number out of what a user typed: a bare number, a "#123", or a GitHub PR URL
// (…/pull/123). Returns "" when there's no valid PR number. Shared by the Queue and the
// command palette so both parse the same way.
export function prnum(s: string): string {
  const u = s.match(/\/pull\/(\d+)/);
  if (u) return u[1];
  const n = s.trim().replace(/^#/, "");
  return /^\d{1,7}$/.test(n) ? n : "";
}
