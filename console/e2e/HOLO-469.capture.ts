import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

/** The seeded `demo` run of `ticket`, the newest when it has several. */
async function runOf(page: Page, ticket: string): Promise<number> {
  const answer = await page.request.get("/projects/demo/history?since=0");
  expect(answer.ok()).toBe(true);
  const { rows } = (await answer.json()) as { rows: { id: number; ticket: string }[] };
  const ids = rows.filter((row) => row.ticket === ticket).map((row) => row.id);
  expect(ids.length).toBeGreaterThan(0);
  return Math.max(...ids);
}

const progress = (page: Page) => page.getByRole("region", { name: "Progress" });

test("01 Progress names the operator seat's steer as operator and the maintainer's as You", async ({ page }) => {
  const run = await runOf(page, "DEMO-7");
  await page.route(new RegExp(`/runs/${run}$`), async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const first = Math.min(...body.events.map((event: { at: number }) => event.at));
    const base = { run, consumed_by: run, interrupt: false, withdrawn: false };
    const notes = [
      { ...base, id: 9001, kind: "amendment", author: "seat-login", note: "change of design for the swap case: keep the comment", at: first + 3 * 60_000, consumed_ms: first + 4 * 60_000 },
      { ...base, id: 9002, kind: "amendment", author: "maintainer", note: "don't add a new flag for the dry run; reuse --act", at: first + 5 * 60_000, consumed_ms: first + 6 * 60_000 },
    ];
    await route.fulfill({ response, json: { ...body, steer_notes: [...body.steer_notes, ...notes] } });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`/next.html#/runs/demo/${run}`);
  const steers = progress(page).locator('li[data-kind="steer"]');
  await expect(steers.filter({ hasText: "operator steered: change of design for the swap case" })).toHaveCount(1);
  await expect(steers.filter({ hasText: "You steered: don't add a new flag for the dry run" })).toHaveCount(1);
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await page.getByRole("link", { name: "history in Progress" }).click();
  await shot(page, "01-progress-operator-steer.png");
});
