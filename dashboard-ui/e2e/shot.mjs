// Dev helper: screenshot an authed SPA page. Usage:
//   URL=http://127.0.0.1:PORT/prbot/pr?pr=38849 COOKIE='wimukthi-sl:exp:sig' OUT=x.png node e2e/shot.mjs
import { chromium } from "@playwright/test";

const url = process.env.URL;
const cookieVal = process.env.COOKIE; // "<login>:<exp>:<sig>"
const out = process.env.OUT || "shot.png";
const u = new URL(url);

const browser = await chromium.launch();
const ctx = await browser.newContext({
  colorScheme: "dark",
  viewport: { width: 1280, height: 1400 },
});
if (cookieVal) {
  await ctx.addCookies([
    { name: "prbot_s", value: cookieVal, domain: u.hostname, path: "/prbot", httpOnly: true },
  ]);
}
const page = await ctx.newPage();
await page.goto(url, { waitUntil: "networkidle" });
await page.waitForTimeout(600);
await page.screenshot({ path: out, fullPage: true });
await browser.close();
console.log("wrote", out);
