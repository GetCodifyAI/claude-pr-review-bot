import { defineConfig, devices } from "@playwright/test";
import { FIXTURE, PORT, SECRET } from "./e2e/fixture";

const BASE = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: true,
  workers: 2,
  expect: { timeout: 10_000 },
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 1,
  reporter: process.env.CI ? "line" : "list",
  use: {
    baseURL: BASE,
    storageState: "./e2e/.auth.json",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // Build the fixture (so the server boots against a ready .env/users.json), build the SPA
    // bundle, then serve it (SPA on) against the offline fixture. The fake gh in the fixture
    // shadows any real gh on PATH so no call reaches GitHub.
    command:
      "node --import tsx e2e/fixture.ts && pnpm build && " +
      `PATH="${FIXTURE}/fakebin:$PATH" ROOT="${FIXTURE}" PRBOT_SECRET="${SECRET}" ` +
      `PRBOT_SPA=1 PRBOT_PORT=${PORT} python3 ../bin/prbot-server.py`,
    url: `${BASE}/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
