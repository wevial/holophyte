import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { historySchema, type HistoryRow } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const now = Date.parse("2026-10-16T12:00:00-07:00");
const newestDay = Date.parse("2026-10-14T00:00:00-07:00");
const minute = 60_000;

function rowsFor(since: number): HistoryRow[] {
  const day = Math.round((newestDay - since) / 86400000);
  const outcomes = day < 1 ? ["merged", "merged", "failed"] : ["merged", "merged"];
  return outcomes.map((outcome, index) => ({
    id: 240 - day * 3 - index,
    ticket: `DEMO-${240 - day * 3 - index}`,
    ticket_url: null,
    title: outcome === "failed" ? "Check the build before landing a change" : "Keep the daily run record",
    attempt: 1,
    phase: "done",
    outcome,
    outcome_reason: null,
    failure_kind: outcome === "failed" ? "verify" : null,
    park_kind: null,
    started_ms: since + (9 + index) * 60 * minute,
    ended_ms: since + (9 + index) * 60 * minute + 24 * minute,
    elapsed_ms: 24 * minute,
    working_ms: 24 * minute,
    estimate_ms: 30 * minute,
    heartbeat_age_ms: null,
    rounds: 1,
    max_rounds: 3,
    pr_url: null,
    host: "writer-host",
  }));
}

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("History outcomes, empty project and the Runs entry link", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "dark",
    timezoneId: "America/Los_Angeles",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  await page.clock.setFixedTime(now);
  await page.route("**/history?**", async route => {
    const query = new URL(route.request().url()).searchParams;
    const since = Number(query.get("since"));
    const until = Number(query.get("until"));
    const limit = Number(query.get("limit"));
    const rows = rowsFor(since);
    await route.fulfill({ json: historySchema.parse({
      rows: limit === 1 ? rows.slice(0, 1) : rows,
      since, until, limit, truncated: limit === 1, total: limit === 1 ? 62 : rows.length, now,
    }) });
  });
  await page.goto("/next.html#/runs/history?outcome=failed");
  await expect(page.getByRole("button", { name: "Failed 1", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("No failed runs this day.")).toHaveCount(4);
  await expect(page.getByText("the bar runs to 1.5× the estimate; the tick marks the estimate")).toBeVisible();
  await expect(page.getByText("times PDT", { exact: true })).toBeInViewport();
  await shot(page, "01-history-failed-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await shot(page, "02-history-failed-dawn.png");
  await page.goto("/next.html#/runs/history?project=demo&outcome=abandoned");
  await expect(page.getByText("No abandoned runs in demo from Oct 14 to Oct 10. Try older days.", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0);
  await shot(page, "03-history-none.png");
  await page.goto("/next.html#/runs?project=demo");
  const older = page.getByRole("link", { name: "Older days", exact: true });
  await expect(older).toHaveAttribute("href", /#\/runs\/history\?project=demo$/);
  await expect(page.getByText("times PDT", { exact: true })).toBeVisible();
  await shot(page, "04-runs-older-link.png");
  await older.click();
  await page.getByRole("button", { name: "Failed 1", exact: true }).click();
  await expect(page).toHaveURL(/outcome=failed/);
  await page.setViewportSize({ width: 390, height: 900 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "05-history-filter-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "history-outcome-flow.webm"));
    await video.delete();
  }
});
