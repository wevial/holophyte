import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import type { RunDetail } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const minute = 60_000;

/** The seeded `demo` run of `ticket`, the newest when it has several. */
async function runOf(page: Page, ticket: string): Promise<number> {
  const answer = await page.request.get("/projects/demo/history?since=0");
  expect(answer.ok()).toBe(true);
  const { rows } = (await answer.json()) as { rows: { id: number; ticket: string }[] };
  const ids = rows.filter((row) => row.ticket === ticket).map((row) => row.id);
  expect(ids.length).toBeGreaterThan(0);
  return Math.max(...ids);
}

/** `body` as a run that used its three review rounds, then passed the terminal adjudication. */
function adjudicated(body: RunDetail): RunDetail {
  const start = Math.min(body.run.started_ms, ...body.events.map((event) => event.at));
  const at = (minutes: number) => start + minutes * minute;
  const [template] = body.rounds;
  expect(template, "the seeded run has a review round to copy").toBeTruthy();
  const finding = { ...(template!.findings[0] ?? { severity: "p1", message: "", path: null, line: null }), severity: "p1" };
  const review = (round: number, begins: number, message: string) => ({
    ...template!, round, reviewer_model: "codex gpt-6-sol", started_ms: at(begins), ended_ms: at(begins + 2),
    verdict: "changes_requested", findings: [{ ...finding, message }],
  });
  const events = [
    { at: at(0), kind: "phase_change", summary: "claimed -> working" },
    { at: at(6), kind: "phase_change", summary: "working -> verifying" },
  ];
  [8, 14, 20].forEach((begins, index) => {
    events.push({ at: at(begins), kind: "phase_change", summary: `verifying -> reviewing: round ${index + 1} review` });
    events.push({ at: at(begins + 2), kind: "phase_change", summary: "reviewing -> addressing" });
    events.push({ at: at(begins + 5), kind: "phase_change", summary: "addressing -> verifying" });
  });
  events.push({ at: at(26), kind: "phase_change", summary: "verifying -> reviewing: terminal adjudication" });
  events.push({ at: at(28), kind: "phase_change", summary: "reviewing -> merge_gate" });
  return {
    ...body,
    run: { ...body.run, max_rounds: 3, ended_ms: at(29) },
    events,
    rounds: [
      review(1, 8, "the dry run writes a row"),
      review(2, 14, "the sweep skips expired leases"),
      review(3, 20, "the lease count is off by one"),
      { ...template!, round: 4, reviewer_model: "claude-adjudicate", started_ms: at(26) + 2, ended_ms: at(27), verdict: "pass", findings: [] },
    ],
    steer_notes: [],
  };
}

const progress = (page: Page) => page.getByRole("region", { name: "Progress" });

test("01 the final check after the third review round, in Dawn", async ({ page }) => {
  const run = await runOf(page, "DEMO-5");
  await page.route(new RegExp(`/runs/${run}$`), async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: adjudicated((await response.json()) as RunDetail) });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`/next.html#/runs/demo/${run}`);
  const lines = progress(page).locator("[data-line-text]");
  await expect(lines.filter({ hasText: "Final check: passed" })).toHaveCount(1);
  await expect(lines.filter({ hasText: "Final check started" })).toHaveCount(1);
  await expect(lines.filter({ hasText: "Review round 4" })).toHaveCount(0);
  const texts = await lines.allTextContents();
  expect(texts.indexOf("Final check started")).toBeGreaterThan(texts.findIndex((text) => text.startsWith("Review round 3:")));
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await progress(page).scrollIntoViewIfNeeded();
  await lines.filter({ hasText: "Final check: passed" }).scrollIntoViewIfNeeded();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-progress-final-check.png") });
});
