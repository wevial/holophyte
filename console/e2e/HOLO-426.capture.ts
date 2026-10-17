import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { historySchema, type HistoryRow } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const now = Date.parse("2026-10-16T12:00:00-07:00");
const minute = 60_000;

function rowsFor(since: number): HistoryRow[] {
  return ["merged", "merged", "merged", "abandoned"].map((outcome, index) => ({
    id: Math.floor(since / 86400000) * 10 + index,
    ticket: `DEMO-${index + 1}`,
    ticket_url: null,
    title: "Keep the daily run record",
    attempt: 1,
    phase: "done",
    outcome,
    outcome_reason: null,
    failure_kind: null,
    park_kind: null,
    started_ms: since + (9 + index) * 60 * minute,
    ended_ms: since + (9 + index) * 60 * minute + 29 * minute,
    elapsed_ms: 29 * minute,
    working_ms: 29 * minute,
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

test("Runs history in night, dawn, older days and narrow viewport", async ({ browser, baseURL }) => {
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
      since, until, limit, truncated: limit === 1, total: limit === 1 ? 159 : rows.length, now,
    }) });
  });
  await page.goto("/next.html#/runs/history");
  await expect(page.getByRole("heading", { level: 1, name: "Runs history" })).toBeVisible();
  await expect(page.getByText("all projects · 159 runs in the 30 days before yesterday · newest day first")).toBeVisible();
  await expect(page.getByRole("heading", { level: 2 })).toHaveText([
    "Wed 14 Oct", "Tue 13 Oct", "Mon 12 Oct", "Sun 11 Oct", "Sat 10 Oct",
  ]);
  await expect(page.getByText("1h 56m of work against 2h 00m estimated")).toHaveCount(5);
  await expect(page.getByRole("button", { name: "Newer days" })).toBeDisabled();
  await shot(page, "01-history-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await shot(page, "02-history-dawn.png");
  await page.getByRole("button", { name: "Older days" }).click();
  await expect(page.getByText("Oct 9 to Oct 5 · page 2 of 6")).toBeVisible();
  await expect(page.getByRole("button", { name: "Newer days" })).toBeEnabled();
  await shot(page, "03-history-page-2.png");
  await page.getByRole("button", { name: "Newer days" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Wed 14 Oct" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 900 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "04-history-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "history-flow.webm"));
    await video.delete();
  }
});
