import path from "node:path";
import { expect, test, type Page, type Route } from "@playwright/test";
import { historySchema, type HistoryRow } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const now = Date.parse("2026-10-16T16:53:00-07:00");
const loadingDay = Date.parse("2026-10-14T00:00:00-07:00");
const faultDay = Date.parse("2026-10-12T00:00:00-07:00");
const cutDay = Date.parse("2026-10-13T00:00:00-07:00");
const minute = 60_000;

function rowsFor(since: number): HistoryRow[] {
  const day = Math.round((loadingDay - since) / 86400000);
  return ["merged", "failed"].map((outcome, index) => ({
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
  await page.screenshot({ path: path.join(out, name) });
}

test("History loading, fault, retry and cut day", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "dark",
    timezoneId: "America/Los_Angeles",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  await page.clock.setFixedTime(now);
  let held: Route | undefined;
  let retry = false;
  let showCut = false;
  await page.route("**/history?**", async route => {
    const query = new URL(route.request().url()).searchParams;
    const since = Number(query.get("since"));
    const until = Number(query.get("until"));
    const limit = Number(query.get("limit"));
    if (since === loadingDay && limit !== 1 && !retry) {
      held = route;
      return;
    }
    if (since === faultDay && limit !== 1) {
      await route.fulfill({ status: 500, body: "store unavailable" });
      return;
    }
    const rows = rowsFor(since);
    const truncated = limit === 1 || (showCut && since === cutDay);
    await route.fulfill({ json: historySchema.parse({
      rows: limit === 1 ? rows.slice(0, 1) : rows,
      since, until, limit, truncated, total: limit === 1 ? 672 : truncated ? 612 : rows.length, now,
    }) });
  });
  await page.goto("/next.html#/runs/history");
  const day = page.getByRole("region", { name: "Wed 14 Oct", exact: true });
  await expect(day).toHaveAttribute("aria-busy", "true");
  await expect(page.getByRole("region", { name: "Tue 13 Oct", exact: true }).getByRole("list")).toBeVisible();
  await expect.poll(() => held !== undefined).toBe(true);
  const fault = page.getByRole("region", { name: "Mon 12 Oct", exact: true });
  await expect(fault.getByRole("status")).toContainText("The run store for demo did not answer.");
  await expect(day.getByRole("list")).toHaveCount(0);
  await expect(fault.getByRole("list")).toHaveCount(0);
  await shot(page, "01-history-loading.png");
  await held!.fulfill({ status: 500, body: "store unavailable" });
  await expect(day.getByRole("status")).toContainText("The run store for demo did not answer.");
  await expect(day.getByRole("status")).toContainText("asked for Wed 14 Oct at 16:53 · showing nothing rather than part of a day");
  await expect(day.getByRole("button", { name: "Try again" })).toBeVisible();
  await shot(page, "02-history-fault-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await shot(page, "03-history-fault-dawn.png");
  retry = true;
  await day.getByRole("button", { name: "Try again" }).click();
  await expect(day.getByRole("status")).toHaveCount(0);
  await expect(day.getByRole("list")).toBeVisible();
  showCut = true;
  await page.reload();
  await expect(page.getByText("newest 500 of 612 shown", { exact: true })).toBeVisible();
  await shot(page, "04-history-cut-day.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "history-retry-flow.webm"));
    await video.delete();
  }
});
