import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function runOf(page: Page, ticket: string): Promise<number> {
  const answer = await page.request.get("/projects/demo/history?since=0");
  expect(answer.ok()).toBe(true);
  const { rows } = (await answer.json()) as { rows: { id: number; ticket: string }[] };
  const ids = rows.filter((row) => row.ticket === ticket).map((row) => row.id);
  expect(ids.length).toBeGreaterThan(0);
  return Math.max(...ids);
}

test("01 a merge gate run in a check fix round says it is fixing CI", async ({ page }) => {
  const run = await runOf(page, "DEMO-7");
  await page.route(/\/projects\/demo\/status$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const runs = body.runs.map((row: { id: number }) => (row.id === run ? { ...row, phase: "merge_gate" } : row));
    await route.fulfill({ response, json: { ...body, runs } });
  });
  await page.route(new RegExp(`/runs/${run}$`), async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const last = Math.max(...body.events.map((event: { at: number }) => event.at));
    const events = [
      ...body.events,
      { at: last + 1_000, kind: "babysit_step", summary: "checks" },
      { at: last + 2_000, kind: "check_fix_round", summary: "check fix round 1 of 2 on https://github.com/o/r/pull/7: vitest; failing: 2 tests red" },
      { at: last + 3_000, kind: "babysit_step", summary: "fix" },
    ];
    await route.fulfill({ response, json: { ...body, run: { ...body.run, phase: "merge_gate" }, events } });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`/next.html#/runs/demo/${run}`);
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  const main = page.getByRole("main");
  await expect(main.getByRole("status").first()).toContainText("Fixing CI: vitest failing (check fix round 1 of 2).");
  await expect(page.getByRole("group", { name: "Stage" })).toContainText("Merge · fixing CI");
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-run-header-fixing-ci.png") });
});
