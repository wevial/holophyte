import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const OPEN_MS = 30_000;

// The real clock: a faked `performance.now` gives zero-length renders, which
// React skips when recording, and that would hide the entries counted here.
async function measuresAfterOpen(page: Page, url: string, ready: Locator): Promise<number> {
  const response = await page.goto(url);
  expect(response?.ok()).toBe(true);
  await expect(ready).toBeVisible();
  await page.waitForTimeout(OPEN_MS);
  return page.evaluate(() => performance.getEntriesByType("measure").length);
}

test.setTimeout(OPEN_MS + 30_000);

test("01 the Overview renders the seeded fleet", async ({ page }) => {
  await page.goto("/next.html#/");
  await expect(page.getByText("Page the shipped ledger").first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-overview.png") });
});

test("02 the new console records no performance measures over 30 s open", async ({ page }) => {
  expect(await measuresAfterOpen(page, "/next.html#/", page.getByText("Page the shipped ledger").first())).toBe(0);
});

test("03 the classic console records no performance measures over 30 s open", async ({ page }) => {
  expect(await measuresAfterOpen(page, "/?classic", page.getByRole("button", { name: "Board", exact: true }))).toBe(0);
});
