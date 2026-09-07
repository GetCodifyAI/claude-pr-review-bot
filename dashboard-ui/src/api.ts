// Typed client for Robin's JSON API (/prbot/api/*). Same-origin, so the session cookie rides
// along automatically. Read endpoints hand back the signed exp/sig tokens for the actions
// available on a resource; POST endpoints pass those back (mirrors the old HTML forms).

const BASE = "/prbot/api";

export class Unauthorized extends Error {}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { credentials: "same-origin", ...init });
  if (r.status === 401) throw new Unauthorized();
  const data = (await r.json().catch(() => ({}))) as T & { error?: string };
  if (!r.ok) throw new Error(data?.error || `${path} → ${r.status}`);
  return data as T;
}

export function get<T>(path: string): Promise<T> {
  return req<T>(path);
}

export function post<T>(path: string, body: unknown = {}): Promise<T> {
  return req<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---- shapes -------------------------------------------------------------------------------

export interface Me {
  authed: boolean;
  login?: string;
  name?: string;
  slack_id?: string;
  claude_connected?: boolean;
  active_skill?: "own" | "team";
  skill_label?: string;
  dry_run: boolean;
  repo: string;
  brand: string;
  oauth: boolean;
}

export interface Token {
  exp: string;
  sig: string;
}

export interface SevChip {
  kind: string;
  n: number;
  label: string;
}

export interface QueueRow {
  num: string;
  title: string;
  author: string;
  state: string;
  size: string;
  when: string[];
  sev: SevChip[];
  archived: boolean;
  archiveToken: Token;
}

export interface QueueTab {
  key: string;
  label: string;
  count: number;
}

export interface QueueData {
  tab: string;
  sort: string;
  tabs: QueueTab[];
  stats: Record<string, number>;
  tabDesc: string;
  rows: QueueRow[];
  slackOk: boolean;
}

export const api = {
  me: () => get<Me>("/me"),
  queue: (tab: string, sort: string) =>
    get<QueueData>(`/queue?tab=${encodeURIComponent(tab)}&sort=${encodeURIComponent(sort)}`),
  login: (pat: string) => post<{ ok: boolean; login: string }>("/login", { pat }),
  logout: () => post<{ ok: boolean }>("/logout"),
};
