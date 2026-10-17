import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { historySchema, type HistoryRow } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const now = Date.parse("2026-10-16T12:00:00-07:00");
const minute = 60_000;
const newestDay = Date.parse("2026-10-14T00:00:00-07:00");

function rowsFor(since: number): HistoryRow[] {
  const examples = since === newestDay ? [
    { id: 213, ticket: "DEMO-215", title: "Stop a run that edits outside its scope", outcome: "merged", work: 26, estimate: 30, hour: 19, endMinute: 45, rounds: 2, pr: 483 },
    { id: 212, ticket: "DEMO-214", title: "Keep the run history across restarts", outcome: "merged", work: 35, estimate: 30, hour: 18, endMinute: 20, rounds: 3, pr: 482 },
    { id: 210, ticket: "DEMO-212", title: "Refresh the project status after a run", outcome: "abandoned", work: 12, estimate: 20, hour: 16, endMinute: 10, rounds: 0, pr: null },
    { id: 208, ticket: "DEMO-210", title: "Check the build before landing a change", outcome: "failed", work: 22, estimate: 30, hour: 14, endMinute: 35, rounds: 1, pr: null },
  ] : [
    { id: 195 - Math.round((newestDay - since) / 86400000), ticket: "DEMO-194", title: "Show the working time against its estimate", outcome: "merged", work: 72, estimate: 45, hour: 17, endMinute: 5, rounds: 2, pr: 470 },
  ];
  return examples.map(example => ({
    id: example.id,
    ticket: example.ticket,
    ticket_url: null,
    title: example.title,
    attempt: 1,
    phase: "done",
    outcome: example.outcome,
    outcome_reason: example.outcome === "abandoned" ? "superseded by a newer run\nrun 214 claimed it" : null,
    failure_kind: example.outcome === "failed" ? "verify" : null,
    park_kind: null,
    started_ms: since + (example.hour * 60 + example.endMinute - example.work) * minute,
    ended_ms: since + (example.hour * 60 + example.endMinute) * minute,
    elapsed_ms: example.work * minute,
    working_ms: example.work * minute,
    estimate_ms: example.estimate * minute,
    heartbeat_age_ms: null,
    rounds: example.rounds,
    max_rounds: 3,
    pr_url: example.pr == null ? null : `https://github.com/example/demo/pull/${example.pr}`,
    host: "writer-host",
  }));
}

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("History run rows in night, dawn and a narrow scrolling day", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "dark",
    timezoneId: "America/Los_Angeles",
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
  const day = page.getByRole("region", { name: "Wed 14 Oct", exact: true });
  await expect(day.getByText("PR #483", { exact: true })).toBeVisible();
  await expect(day.getByText("at verify", { exact: true })).toBeVisible();
  await expect(day.getByText("superseded by a newer run", { exact: true })).toBeVisible();
  await expect(day.getByText("+5m", { exact: true })).toBeVisible();
  await shot(page, "01-history-rows-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await shot(page, "02-history-rows-dawn.png");
  await page.setViewportSize({ width: 390, height: 900 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const table = day.locator("[data-day-run-table]");
  await expect.poll(() => table.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  await table.evaluate(element => { element.scrollLeft = element.scrollWidth; });
  await expect.poll(() => table.evaluate(element => element.scrollLeft > 0)).toBe(true);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "03-history-rows-390.png");
  await context.close();
});
