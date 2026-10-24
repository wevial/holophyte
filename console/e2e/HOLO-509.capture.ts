import path from "node:path";
import { expect, test } from "@playwright/test";
import { historySchema, type HistoryRow } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const now = Date.parse("2026-10-16T12:00:00-07:00");
const newestDay = Date.parse("2026-10-14T00:00:00-07:00");
const minute = 60_000;
const repo = "https://github.com/example/demo";

const merges = [
  { id: 241, title: "Show the run's chain on its page", hour: 9, work: 14, estimate: 30, rounds: 1, findings: 2, additions: 86, deletions: 12, pr: 812, sha: "3f9c2a71d04be5a6c7d8e9f0a1b2c3d4e5f60718" },
  { id: 242, title: "Count a parked run's wait apart from its work", hour: 11, work: 27, estimate: 30, rounds: 2, findings: 3, additions: 240, deletions: 31, pr: 815, sha: "8b17e0c4a9f2d3e4f5a6b7c8d9e0f1a2b3c4d5e6" },
  { id: 243, title: "Fold a ticket's follow-ups into the findings ledger", hour: 14, work: 41, estimate: 30, rounds: 3, findings: 6, additions: 512, deletions: 140, pr: 819, sha: "c4e5d6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3" },
  { id: 244, title: "Bump the review model's default", hour: 16, work: 9, estimate: 20, rounds: 1, findings: 1, additions: null, deletions: null, pr: null, sha: "e1d2c3b4a5f60718293a4b5c6d7e8f9012345678" },
];

function rowsFor(since: number): HistoryRow[] {
  if (since !== newestDay) return [];
  return merges.map(merge => ({
    id: merge.id, ticket: `DEMO-${merge.id}`, ticket_url: null, title: merge.title, attempt: 1,
    phase: "done", outcome: "merged", outcome_reason: null, failure_kind: null, park_kind: null,
    started_ms: since + merge.hour * 60 * minute, ended_ms: since + merge.hour * 60 * minute + (merge.work + 6) * minute,
    elapsed_ms: (merge.work + 6) * minute, working_ms: merge.work * minute, estimate_ms: merge.estimate * minute,
    heartbeat_age_ms: null, rounds: merge.rounds, max_rounds: 3, findings: merge.findings,
    merge_sha: merge.sha, commit_url: `${repo}/commit/${merge.sha}`,
    additions: merge.additions, deletions: merge.deletions,
    pr_url: merge.pr == null ? null : `${repo}/pull/${merge.pr}`, host: "writer-host",
  })).reverse();
}

test("History merged rows read the time box bar, findings and change", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light", timezoneId: "America/Los_Angeles",
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
      since, until, limit, truncated: false, total: limit === 1 ? 4 : rows.length, now,
    }) });
  });
  await page.goto("/next.html#/runs/history?outcome=merged");
  const day = page.getByRole("region", { name: "Wed 14 Oct", exact: true });
  await expect(day.getByRole("listitem")).toHaveCount(4);
  await expect(day.locator("[role='progressbar'][data-tone='ok']")).toHaveCount(2);
  await expect(day.locator("[role='progressbar'][data-tone='warn']")).toHaveCount(1);
  await expect(day.locator("[role='progressbar'][data-tone='over']")).toHaveCount(1);
  await expect(day.getByText("+11m", { exact: true })).toBeVisible();
  await expect(day.getByRole("link", { name: "PR #819" })).toBeVisible();
  await expect(day.getByRole("link", { name: "e1d2c3b" })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-history-merged-facts.png"), animations: "disabled" });
  await context.close();
});
