import { expect, test } from "@playwright/test";
import { PR } from "./fixture";

// Unauthenticated: the SPA shell mounts and shows the login screen (no session cookie).
test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("shows the login screen", async ({ page }) => {
    await page.goto("/prbot/");
    await expect(page.getByRole("button", { name: /sign in|connect|log in/i }).first()).toBeVisible();
    await expect(page.locator("input")).toBeVisible();
  });
});

// Authenticated via the injected session cookie (see global-setup / fixture).
test.describe("signed in", () => {
  test("queue lists the fixture PR (in its Reviewed tab)", async ({ page }) => {
    await page.goto("/prbot/?tab=reviewed");
    await expect(page.getByRole("heading", { name: /review queue/i })).toBeVisible();
    await expect(page.getByText(`#${PR}`)).toBeVisible();
    await expect(page.getByText(/lead-time badge/i)).toBeVisible();
  });

  test("opens the PR detail with its drafted findings", async ({ page }) => {
    await page.goto(`/prbot/pr?pr=${PR}`);
    await expect(page.locator("h1.prtitle")).toContainText(`#${PR}`);
    await expect(page.locator("h1.prtitle")).toContainText(/lead-time badge/i);
    await expect(page.getByText(/guard against a null vendor/i).first()).toBeVisible();
  });

  test("skills page renders", async ({ page }) => {
    await page.goto("/prbot/skills");
    await expect(page.getByRole("heading", { name: /review skills/i })).toBeVisible();
  });

  test("integrations page renders the connect cards", async ({ page }) => {
    await page.goto("/prbot/integrations");
    await expect(page.getByRole("heading", { name: /integrations/i })).toBeVisible();
    await expect(page.getByText(/connected as/i)).toBeVisible(); // GitHub card
    await expect(page.getByText(/pings you when a review is requested/i)).toBeVisible(); // Slack
    await expect(page.getByText(/required to review/i)).toBeVisible(); // Claude
  });

  test("learnings page renders", async ({ page }) => {
    await page.goto("/prbot/learnings");
    await expect(page.getByRole("heading", { name: /has learned/i })).toBeVisible();
  });

  test("how-it-works page renders the flow", async ({ page }) => {
    await page.goto("/prbot/how");
    await expect(page.getByRole("heading", { name: /how robin works/i })).toBeVisible();
    await expect(page.getByText(/a review is requested/i)).toBeVisible();
  });

  test("QA index renders", async ({ page }) => {
    await page.goto("/prbot/qa");
    await expect(page.getByRole("heading", { name: /qa guides/i })).toBeVisible();
  });

  test("sidebar navigates between pages", async ({ page }) => {
    await page.goto("/prbot/");
    await page.getByRole("link", { name: /integrations/i }).click();
    await expect(page).toHaveURL(/\/integrations/);
    await expect(page.getByRole("heading", { name: /integrations/i })).toBeVisible();
  });
});
