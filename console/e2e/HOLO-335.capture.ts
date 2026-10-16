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

const block = (page: Page, name: string) => page.getByRole("region", { name });

async function open(page: Page, run: number) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`/next.html#/runs/demo/${run}`);
}

test("01 the CI park's pull request line and ticket summary, in night", async ({ page }) => {
  const run = await runOf(page, "DEMO-8");
  await open(page, run);
  await expect(block(page, "Pull request")).toContainText("PR #41 · checks pending");
  await expect(block(page, "Ticket")).toContainText("Add a CSV export endpoint");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-run-pr-parked.png");
});

test("02 the ready-to-merge park in dawn, its merge readiness answering ready", async ({ page }) => {
  const run = await runOf(page, "DEMO-9");
  await page.route(/\/runs\/\d+\/merge$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: body.ticket === "DEMO-9" ? { ...body, ready: true, reason: null, detail: null } : body });
  });
  await open(page, run);
  await expect(block(page, "Pull request")).toContainText("PR #42 · checks green · approved · no open threads · ready for your merge");
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await shot(page, "02-run-pr-ready-dawn.png");
});

test("03 today's merge, its merged as line", async ({ page }) => {
  const run = await runOf(page, "DEMO-10");
  await open(page, run);
  await expect(block(page, "Pull request")).toContainText(/^Pull requestPR #43 · merged as 0f1e2d3 at \d\d:\d\d$/);
  await shot(page, "03-run-pr-merged.png");
});
