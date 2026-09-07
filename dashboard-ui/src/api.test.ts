// Unit tests for the typed API client. No network: global.fetch is stubbed per case.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, get, post, Unauthorized } from "./api";

type FetchArgs = { url: string; init?: RequestInit };
let calls: FetchArgs[] = [];

function stubFetch(status: number, body: unknown) {
  calls = [];
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    } as Response;
  }) as typeof fetch;
}

afterEach(() => {
  calls = [];
});

test("get hits the /prbot/api base and returns parsed JSON", async () => {
  stubFetch(200, { authed: true, login: "me" });
  const r = await get<{ authed: boolean; login: string }>("/me");
  assert.equal(r.login, "me");
  assert.equal(calls[0].url, "/prbot/api/me");
  assert.equal(calls[0].init?.credentials, "same-origin");
});

test("post serializes the body as JSON with the right header and method", async () => {
  stubFetch(200, { ok: true });
  await post("/review", { pr: "42", effort: "deep" });
  const init = calls[0].init!;
  assert.equal(init.method, "POST");
  assert.equal((init.headers as Record<string, string>)["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(init.body as string), { pr: "42", effort: "deep" });
});

test("a 401 throws Unauthorized", async () => {
  stubFetch(401, {});
  await assert.rejects(() => get("/queue?tab=todo&sort=newest"), Unauthorized);
});

test("a non-ok response throws with the server error message", async () => {
  stubFetch(403, { error: "bad token" });
  await assert.rejects(() => post("/approve", {}), /bad token/);
});

test("api.queue encodes tab and sort into the query string", async () => {
  stubFetch(200, { rows: [] });
  await api.queue("to do", "oldest first");
  assert.equal(calls[0].url, "/prbot/api/queue?tab=to%20do&sort=oldest%20first");
});

test("api.pr appends the version param only when given", async () => {
  stubFetch(200, {});
  await api.pr("42");
  assert.equal(calls[0].url, "/prbot/api/pr?pr=42");
  await api.pr("42", "3");
  assert.equal(calls[1].url, "/prbot/api/pr?pr=42&v=3");
});

test("api.skillAction posts to the step-scoped route", async () => {
  stubFetch(200, { bannerHtml: "<div/>" });
  await api.skillAction("save", { target: "global", skill: "x" });
  assert.equal(calls[0].url, "/prbot/api/skill/save");
});
