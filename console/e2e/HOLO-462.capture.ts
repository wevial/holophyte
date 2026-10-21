import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { ledgerSchema, type LedgerEntry } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const minute = 60_000;
const notes = [
  ["merge", "DEMO-2 · Merged the board's ready column"],
  ["failure", "DEMO-6 · Verify failed: the build exited 1"],
  ["intervention", "DEMO-7 · Requested changes on the review"],
  ["note", "DEMO-4 · Claimed and started implementing"],
] as const;

function entries(now: number): LedgerEntry[] {
  return Array.from({ length: 24 }, (_, index) => {
    const [kind, text] = notes[index % notes.length]!;
    return {
      at: now - (index + 1) * 17 * minute, kind, text,
      source: kind === "intervention" ? "operator" : "factory",
      run: null, ticket: null,
    };
  });
}

async function scrolledLogShot(page: Page, name: string) {
  const log = page.getByRole("complementary", { name: "Today's log", exact: true });
  const heading = log.getByRole("heading", { name: "Today's log", exact: true });
  await expect(log.getByRole("listitem").first()).toBeVisible();
  const scrollable = await log.evaluate(element => element.scrollHeight > element.clientHeight);
  expect(scrollable, "the served day log overflows its column").toBe(true);
  await log.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect.poll(() => log.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  const logBox = (await log.boundingBox())!;
  const headingBox = (await heading.boundingBox())!;
  expect(Math.abs(headingBox.y - logBox.y)).toBeLessThanOrEqual(1);
  await expect(heading).toBeInViewport();
  const background = await heading.evaluate(element => getComputedStyle(element).backgroundColor);
  expect(background).not.toBe("transparent");
  expect(background).not.toMatch(/^rgba\(\d+, \d+, \d+, 0\)$/);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

test("the day log's heading stays at the top of the scrolled log", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.route("**/ledger?**", async route => {
    const query = new URL(route.request().url()).searchParams;
    await route.fulfill({ json: ledgerSchema.parse({
      entries: entries(Date.now()), active_outages: [],
      since: Number(query.get("since")), limit: Number(query.get("limit")),
    }) });
  });
  await page.goto("/next.html#/settings/appearance");
  await page.getByRole("radio", { name: "Dawn", exact: true }).check();
  await page.getByRole("link", { name: /^Overview/ }).click();
  await scrolledLogShot(page, "01-day-log-scrolled-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await scrolledLogShot(page, "02-day-log-scrolled-night.png");
});
