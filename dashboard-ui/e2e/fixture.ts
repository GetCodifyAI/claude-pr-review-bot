// Builds a self-contained, offline fixture for the Playwright e2e run and mints a matching
// session cookie. Run standalone (`node e2e/fixture.mjs`) — the Playwright webServer runs it
// before booting the Python server, and global-setup runs it before the workers start, so the
// server always boots against a ready fixture regardless of Playwright's setup/webServer order.
// A FIXED (non-secret) test secret keeps the server's .env and the minted cookie in agreement.
import { createHmac } from "node:crypto";
import { chmodSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const FIXTURE = join(HERE, ".fixture");
export const AUTH_STATE = join(HERE, ".auth.json");
export const SECRET = "e2e-fixed-test-secret-not-for-production";
export const USER = "wimukthi-sl";
export const PR = "38849";
export const PR2 = "38850"; // dedicated archive-test target — no other test touches it
export const PORT = 8988;

function write(path: string, body: string) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, body);
}

export function buildFixture() {
  rmSync(FIXTURE, { recursive: true, force: true });
  mkdirSync(FIXTURE, { recursive: true });

  write(
    join(FIXTURE, ".env"),
    [
      `PRBOT_SECRET=${SECRET}`,
      `REVIEWER=${USER}`,
      "REPO=GetCodifyAI/cut-and-dry",
      "DRY_RUN=1",
      "PRBOT_ENV=e2e",
      "PUBLIC_URL=https://prbot-e2e.example.com",
      "GITHUB_PAT=ghp_e2e_dummy_never_used",
    ].join("\n") + "\n",
  );

  write(
    join(FIXTURE, "users.json"),
    JSON.stringify({ [USER]: { name: "Wimukthi", slack_id: "U0TEST", added: 1, updated: 1 } }),
  );

  write(
    join(FIXTURE, "queue.json"),
    JSON.stringify([
      {
        number: Number(PR),
        title: "Add lead-time badge to product cards",
        url: `https://github.com/GetCodifyAI/cut-and-dry/pull/${PR}`,
        additions: 42,
        deletions: 8,
        changedFiles: 5,
        requested: [USER],
        author: "teammate",
        isBot: false,
        isDraft: false,
        head: "deadbeefcafe",
        createdAt: "2026-05-01T10:00:00Z",
        updatedAt: "2026-05-02T10:00:00Z",
      },
      {
        number: Number(PR2),
        title: "Cache vendor lead times",
        url: `https://github.com/GetCodifyAI/cut-and-dry/pull/${PR2}`,
        additions: 12,
        deletions: 3,
        changedFiles: 2,
        requested: [USER],
        author: "teammate",
        isBot: false,
        isDraft: false,
        head: "feedfacecafe",
        createdAt: "2026-05-03T10:00:00Z",
        updatedAt: "2026-05-04T10:00:00Z",
      },
    ]),
  );

  write(join(FIXTURE, "state", PR2, "status"), "done");
  write(
    join(FIXTURE, "state", PR2, "review.json"),
    JSON.stringify({
      event: "COMMENT",
      summary: "Caches vendor lead times. Looks fine.",
      explainer: "",
      analysis: "",
      comments: [],
    }),
  );

  write(join(FIXTURE, "state", PR, "status"), "done");
  write(
    join(FIXTURE, "state", PR, "review.json"),
    JSON.stringify({
      event: "COMMENT",
      summary: "Adds a lead-time badge to product cards. Logic is sound; two small things.",
      explainer: "A COMMENT review — nothing here blocks the merge.",
      analysis: "",
      comments: [
        {
          path: "app/models/Product.php",
          line: 42,
          severity: "should-fix",
          body: "Guard against a null vendor before reading its lead time.",
          reply_to: null,
          suggestion: "if ($vendor === null) return null;",
        },
        {
          path: "src/javascripts/Badge.tsx",
          line: 10,
          severity: "nit",
          body: "Prefer `const` over `let` here.",
          reply_to: null,
          suggestion: "",
        },
      ],
    }),
  );

  // Fake gh: every call fails, so gh_json() returns its defaults and the server renders from
  // the on-disk fixture. Placed on PATH ahead of any real gh by the webServer command.
  const gh = join(FIXTURE, "fakebin", "gh");
  write(gh, "#!/bin/sh\nexit 1\n");
  chmodSync(gh, 0o755);
}

export function mintAuthState() {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  const sig = createHmac("sha256", SECRET).update(`session:${USER}:${exp}`).digest("hex");
  writeFileSync(
    AUTH_STATE,
    JSON.stringify({
      cookies: [
        {
          name: "prbot_s",
          value: `${USER}:${exp}:${sig}`,
          domain: "127.0.0.1",
          path: "/",
          expires: exp,
          httpOnly: true,
          secure: false,
          sameSite: "Lax",
        },
      ],
      origins: [],
    }),
  );
}

// Standalone invocation (from the webServer command).
if (process.argv[1] && process.argv[1]?.endsWith("fixture.ts")) {
  buildFixture();
  mintAuthState();
  console.log("e2e fixture built at", FIXTURE);
}
