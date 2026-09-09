// Typed client for Robin's JSON API (/api/*). Same-origin, so the session cookie rides
// along automatically. Read endpoints hand back the signed exp/sig tokens for the actions
// available on a resource; POST endpoints pass those back (mirrors the old HTML forms).

const BASE = "/api";

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
  logo?: string;
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

export interface Finding {
  i: number;
  severity: string;
  sevLabel: string;
  path: string;
  line: number | string;
  thread: string | null;
  body: string;
  suggestion: string;
  low: boolean;
  title: string;
  impact: string;
  structured: boolean;
  agreement?: { confirmed: boolean; n: number; by: string[]; differ: string } | null;
}

export interface ApprovedData {
  at: string;
  ago: string;
  manual: boolean;
  body: string;
  user: string;
}

export interface ReviewData {
  event: string;
  summary: string;
  explainer: string;
  analysis: string;
  chips: SevChip[];
  findings: Finding[];
  count: number;
  posted: boolean;
  postLabel: string;
  reused?: boolean;
  convergence?: { rate: number | null; confirmed: number; total: number; nRuns: number } | null;
  approve?: { lgtm: boolean; blockers: number; defaultMsg: string };
  approved?: ApprovedData;
}

export interface Reviewer { login: string; state: string }
export interface ReviewersData { reviewers: Reviewer[]; decision: string | null }
export interface TimelineStep { label: string; done: boolean; note: string }
export interface EffortLevel { key: string; name: string; sub: string }
export interface OtherRun {
  login: string;
  effort: string;
  effortKey: string;
  focus: string;
  model: string;
  skill: string;
  skillKey: string;
  when: string;
}
export interface RunFormData {
  suggested: string;
  levels: EffortLevel[];
  models: EffortLevel[];
  skillLabel: string;
  othersOnHead: OtherRun[];
}
export interface Risk { icon: string; title: string; note: string }
export interface HistoryRun {
  ts: number;
  effort: string;
  focus: string;
  findings: number;
  event: string;
}

export interface Usage {
  model: string;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  realTokens: number;
  costUsd: number;
}

export interface PrData {
  historyView?: boolean;
  ts?: number;
  when?: string;
  pr: string;
  title: string;
  state: string;
  ghUrl: string;
  author: string;
  size: string;
  dryRun: boolean;
  awaiting: boolean;
  runner: string;
  effortBadge: { label: string; hint: string } | null;
  usage?: Usage | null;
  focus: string;
  stale: boolean;
  risk: Risk[];
  timeline: TimelineStep[];
  reviewers: ReviewersData | null;
  claudeConnected: boolean;
  runForm: RunFormData;
  tokens: Record<string, Token>;
  reviewing?: {
    phases: string[];
    cur: number;
    queued: boolean;
    effortLabel: string;
    effortHint: string;
    focus: string;
  };
  stopped?: { halted: boolean };
  stalled?: { was: string; tail: string };
  notReviewed?: boolean;
  failed?: string;
  approved?: ApprovedData;
  review?: ReviewData;
  showMarkDone?: boolean;
  history: HistoryRun[];
  // history view only
  summary?: string;
  findings?: { severity: string; sevLabel: string; path: string; line: number | string; body: string }[];
}

export interface BannerResult {
  bannerHtml: string;
}

export interface QaGuide { num: string; title: string; when: string }
export interface QaIndex { guides: QaGuide[] }
export interface QaDetail {
  pr: string;
  title: string;
  ghUrl: string;
  state: string;
  connected: boolean;
  genToken: Token;
  stopToken?: Token;
  md?: string;
  failed?: string;
  stopped?: boolean;
  running?: { phases: string[]; cur: number; queued: boolean };
}

export interface SkillStat {
  skill: string;
  kept: number;
  edited: number;
  dropped: number;
  total: number;
  rate: number;
}
export interface DepthInfo { name: string; meta: string; content: string; edited: boolean }
export interface SkillsData {
  token: Token;
  user: string;
  choice: "own" | "team";
  effLabel: string;
  hasMySkill: boolean;
  hasGlobal: boolean;
  teamSkill: string;
  mySkill: string;
  depths: Record<string, DepthInfo>;
  stats: SkillStat[];
  teamHistory: { hash: string; author: string; at: number; msg: string }[];
}


export interface IntegrationsData {
  token: Token;
  github: { login: string };
  slack: { id: string };
  claude: { connected: boolean; authUrl: string };
  oauth: boolean;
  brand: string;
}

export interface LearningRow {
  kind: string;
  label: string;
  loc: string;
  severity: string;
  gist: string;
  editedGist: string;
}
export interface LearningsData {
  counts: { dropped: number; edited: number; kept: number };
  rows: LearningRow[];
}

export interface StackItem {
  num: string;
  title: string;
  base: string;
  head: string;
  state: string;
}
export interface StackData {
  pr: string;
  isStack: boolean;
  connected: boolean;
  runToken: Token;
  levels: EffortLevel[];
  stack: StackItem[];
}

export interface HowData {
  images: Record<string, string>;
  brand: string;
  reviewer: string;
  tabs: { key: string; label: string; desc: string }[];
}

export interface RollupSeriesPoint {
  ts: number;
  date: string;
  reviews: number;
  tokens: number;
  kept: number;
  edited: number;
  dropped: number;
}
export interface RollupData {
  generatedAt: number;
  reviews: { total: number; week: number };
  prs: number;
  reviewers: { login: string; runs: number; week: number; tokens: number }[];
  tokens: { total: number; week: number };
  keep: { allTime: { kept: number; edited: number; dropped: number; rate: number | null } };
  severity: { blocker: number; "should-fix": number; nit: number; question: number };
  models: { model: string; runs: number; tokens: number }[];
  agreement: { multiReviewerPRs: number; confirmedFindings: number; avgRate: number | null };
  cycle: { medianReviewToPostSec: number | null; n: number };
  series: RollupSeriesPoint[];
}

export const api = {
  rollup: () => get<RollupData>("/rollup"),
  me: () => get<Me>("/me"),
  queue: (tab: string, sort: string) =>
    get<QueueData>(`/queue?tab=${encodeURIComponent(tab)}&sort=${encodeURIComponent(sort)}`),
  login: (pat: string) => post<{ ok: boolean; login: string }>("/login", { pat }),
  logout: () => post<{ ok: boolean }>("/logout"),
  pr: (pr: string, v?: string) => get<PrData>(`/pr?pr=${pr}${v ? `&v=${v}` : ""}`),
  explain: (pr: string, t: Token, idx: number) =>
    post<{ md: string }>("/explain", { pr, ...t, idx }),
  review: (pr: string, t: Token, effort: string, focus: string, model: string) =>
    post<{ ok: boolean; started?: boolean }>("/review", { pr, ...t, effort, focus, model }),
  stop: (pr: string, t: Token) => post<{ ok: boolean; confirmed: boolean }>("/stop", { pr, ...t }),
  markdone: (pr: string, t: Token) => post<{ ok: boolean }>("/markdone", { pr, ...t }),
  archive: (pr: string, t: Token, action: "archive" | "unarchive") =>
    post<{ ok: boolean }>("/archive", { pr, ...t, action }),
  post: (
    pr: string,
    t: Token,
    payload: { selected: number[]; bodies: Record<number, string>; suggs: Record<number, string>; request_changes: boolean }
  ) => post<BannerResult>("/post", { pr, ...t, ...payload }),
  approve: (pr: string, t: Token, body: string, ack: boolean) =>
    post<BannerResult>("/approve", { pr, ...t, body, ack }),
  qaIndex: () => get<QaIndex>("/qa"),
  qaDetail: (pr: string) => get<QaDetail>(`/qa?pr=${pr}`),
  qaGen: (pr: string, t: Token) => post<{ ok: boolean }>("/qa/gen", { pr, ...t }),
  qaStop: (pr: string, t: Token) => post<{ ok: boolean }>("/qa/stop", { pr, ...t }),
  skills: () => get<SkillsData>("/skills"),
  skillAction: (step: string, payload: Record<string, unknown>) =>
    post<BannerResult>(`/skill/${step}`, payload),
  integrations: () => get<IntegrationsData>("/integrations"),
  saveSettings: (t: Token, fields: { slack_id?: string; pat?: string }) =>
    post<BannerResult>("/settings", { ...t, ...fields }),
  claudeCode: (t: Token, code: string) =>
    post<BannerResult & { connected: boolean }>("/claude/code", { ...t, code }),
  claudeDisconnect: (t: Token) =>
    post<BannerResult & { connected: boolean }>("/claude/disconnect", { ...t }),
  claudeCancel: (t: Token) =>
    post<BannerResult & { connected: boolean }>("/claude/cancel", { ...t }),
  learnings: () => get<LearningsData>("/learnings"),
  stack: (pr: string) => get<StackData>(`/stack?pr=${pr}`),
  stackRun: (pr: string, t: Token, effort: string, nums: string[]) =>
    post<{ ok: boolean; started: number }>("/stack/run", { pr, ...t, effort, nums }),
  how: () => get<HowData>("/how"),
};
