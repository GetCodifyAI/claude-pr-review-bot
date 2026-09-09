import { expect, test } from "@playwright/test";
import { PR, PR2 } from "./fixture";

// Unauthenticated: the SPA shell mounts and shows the login screen (no session cookie).
test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("shows the login screen", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("button", { name: /sign in|connect|log in/i }).first()).toBeVisible();
    await expect(page.locator("input")).toBeVisible();
  });
});

// Authenticated via the injected session cookie (see global-setup / fixture).
test.describe("signed in", () => {
  test("queue lists the fixture PR (in its Reviewed tab)", async ({ page }) => {
    await page.goto("/?tab=reviewed");
    await expect(page.getByRole("heading", { name: /review queue/i })).toBeVisible();
    await expect(page.getByText(`#${PR}`)).toBeVisible();
    await expect(page.getByText(/lead-time badge/i)).toBeVisible();
    // Legacy /prbot URLs must still resolve (old bookmarks + signed Slack links).
    await page.goto("/prbot/?tab=reviewed");
    await expect(page.getByText(`#${PR}`)).toBeVisible();
  });

  test("archive button works: moves a PR to Archived and restores it", async ({ page }) => {
    const rowFor = (num: string) => page.locator(".row", { hasText: `#${num}` });
    await page.goto("/?tab=reviewed");
    await expect(rowFor(PR2)).toBeVisible();

    // Archive it — the row leaves the Reviewed tab.
    await rowFor(PR2).getByRole("button", { name: new RegExp(`archive #${PR2}`, "i") }).click();
    await expect(rowFor(PR2)).toHaveCount(0);

    // It now shows under Archived with a Restore action.
    await page.goto("/?tab=archived");
    await expect(rowFor(PR2)).toBeVisible();
    await rowFor(PR2).getByRole("button", { name: new RegExp(`restore #${PR2}`, "i") }).click();
    await expect(rowFor(PR2)).toHaveCount(0);
  });

  test("opens the PR detail with its drafted findings", async ({ page }) => {
    await page.goto(`/pr?pr=${PR}`);
    await expect(page.locator("h1.prtitle")).toContainText(`#${PR}`);
    await expect(page.locator("h1.prtitle")).toContainText(/lead-time badge/i);
    await expect(page.getByText(/can crash the lead-time badge/i).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /explain simply/i }).first()).toBeVisible();
  });

  test("insights dashboard renders from the rollup api", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { name: /^insights$/i })).toBeVisible();
    await expect(page.getByText(/review activity/i)).toBeVisible();
    await expect(page.getByText(/agreement across reviewers/i)).toBeVisible();
  });

  test("skills page renders", async ({ page }) => {
    await page.goto("/skills");
    await expect(page.getByRole("heading", { name: /review skills/i })).toBeVisible();
  });

  test("integrations page renders the connect cards", async ({ page }) => {
    await page.goto("/integrations");
    await expect(page.getByRole("heading", { name: /integrations/i })).toBeVisible();
    await expect(page.getByText(/connected as/i)).toBeVisible(); // GitHub card
    await expect(page.getByText(/pings you when a review is requested/i)).toBeVisible(); // Slack
    await expect(page.getByText(/required to review/i)).toBeVisible(); // Claude
  });

  test("learnings page renders", async ({ page }) => {
    await page.goto("/learnings");
    await expect(page.getByRole("heading", { name: /has learned/i })).toBeVisible();
  });

  test("how-it-works page renders the flow", async ({ page }) => {
    await page.goto("/how");
    await expect(page.getByRole("heading", { name: /how robin works/i })).toBeVisible();
    await expect(page.getByText(/a review is requested/i)).toBeVisible();
  });

  test("QA index renders", async ({ page }) => {
    await page.goto("/qa");
    await expect(page.getByRole("heading", { name: /qa guides/i })).toBeVisible();
  });

  test("sidebar navigates between pages", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: /integrations/i }).click();
    await expect(page).toHaveURL(/\/integrations/);
    await expect(page.getByRole("heading", { name: /integrations/i })).toBeVisible();
  });

  test("command palette (\u2318K) reviews a PR by number", async ({ page }) => {
    await page.goto("/");
    // Wait for the app to mount before the global ⌘K listener exists (else the keypress is lost
    // on a cold start and the palette never opens).
    await expect(page.getByRole("heading", { name: /review queue/i })).toBeVisible();
    await page.keyboard.press("ControlOrMeta+k");
    await expect(page.locator(".cmdk")).toBeVisible();
    await page.locator(".cmdk-in").fill(PR);
    await expect(page.getByText(`Review PR #${PR}`)).toBeVisible();
    await page.locator(".cmdk-in").press("Enter");
    await expect(page).toHaveURL(new RegExp(`/pr\\?pr=${PR}`));
  });

  test("the Review a PR button opens the palette", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: /review a pr/i }).click();
    await expect(page.locator(".cmdk")).toBeVisible();
  });

  test("How it works is under Help, not the primary nav", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("nav.nav").getByText(/how it works/i)).toHaveCount(0);
    await page.getByRole("button", { name: /help/i }).click();
    await page.getByRole("link", { name: /how it works/i }).click();
    await expect(page).toHaveURL(/\/how/);
    await expect(page.getByRole("heading", { name: /how robin works/i })).toBeVisible();
  });
});
