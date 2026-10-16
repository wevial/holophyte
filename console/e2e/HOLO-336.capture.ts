import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import type { RunCriterion, RunDetail, Status } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const criteria: RunCriterion[] = [
  { n: 1, text: "Silent leases are requeued by the sweep.", status: "met", note: "tests/test_a.py::test_one", run: 12, round: 1 },
  { n: 2, text: "Fresh leases remain active until their deadline.", status: "met", note: "tests/test_a.py::test_two", run: 12, round: 1 },
  { n: 3, text: "The sweep records its work in the run history.", status: "met", note: "tests/test_a.py::test_three", run: 12, round: 1 },
  { n: 4, text: "The history row names sweep as its source.", status: "not_met", note: "the row names loop", run: 12, round: 1 },
];

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

async function open(page: Page, standing: RunCriterion[]) {
  const answer = await page.request.get("/projects/demo/history?since=0");
  expect(answer.ok()).toBe(true);
  const { rows } = await answer.json() as { rows: { id: number; ticket: string }[] };
  const id = Math.max(...rows.filter((row) => row.ticket === "DEMO-7").map((row) => row.id));
  expect(Number.isFinite(id)).toBe(true);
  await page.route(new RegExp(`/runs/${id}$`), async (route) => {
    const response = await route.fetch();
    const body = await response.json() as RunDetail;
    const round = body.rounds[0]!;
    await route.fulfill({ response, json: {
      ...body,
      criteria: standing.map((criterion) => ({ ...criterion, run: id })),
      rounds: standing.length ? [
        { ...round, round: 1, ended_ms: Date.now() - 60_000, verdict: "changes_requested" },
        { ...round, round: 2, ended_ms: null, verdict: "", findings: [], instructions: [], operator_notes: [] },
      ] : [],
    } });
  });
  await page.route(/\/status$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json() as Status;
    await route.fulfill({ response, json: { ...body, runs: body.runs?.map((run) => run.id === id ? { ...run, max_rounds: 3 } : run) } });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/next.html#/runs/demo/${id}`);
  return page.getByRole("region", { name: "Acceptance criteria" });
}

test("reviewing criteria in night and dawn, then their evidence", async ({ page }, testInfo) => {
  const block = await open(page, criteria);
  await expect(block).toContainText("3 of 4 met");
  await expect(block).toContainText("waits on round 2");
  await expect(page.getByRole("main").getByRole("status")).toHaveText("Reviewing round 2 of 3. Three of four criteria are met. Nothing needs you.");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "01-criteria-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await shot(page, "02-criteria-dawn.png");
  await block.locator("summary").click();
  await expect(block.getByText("AC4 · round 1: the row names loop")).toBeVisible();
  await shot(page, "03-criteria-evidence.png");
  const video = page.video();
  await page.close();
  await video?.saveAs(testInfo.outputPath("criteria-flow.webm"));
});

test("criteria before any review has judged them", async ({ page }) => {
  const block = await open(page, []);
  await expect(block).toContainText("No review has judged the criteria yet.");
  await shot(page, "04-criteria-none.png");
});
