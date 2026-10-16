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

const verdict = (page: Page) => page.getByRole("main").getByRole("status");

async function open(page: Page, run: number | string, width = 1440) {
  await page.setViewportSize({ width, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`/next.html#/runs/demo/${run}`);
}

test("01 the reviewing run's header, sentence and stage chip, night and dawn", async ({ page }) => {
  const run = await runOf(page, "DEMO-7");
  await open(page, run);
  await expect(verdict(page)).toHaveText(/^Reviewing round \d of \d\. Nothing needs you\.$/);
  await expect(page.getByRole("group", { name: "Stage" })).toContainText("loop back");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-run-live-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await shot(page, "02-run-live-dawn.png");
});

test("02 the stranded failure's sentence", async ({ page }) => {
  const run = await runOf(page, "DEMO-11");
  await open(page, run);
  await expect(verdict(page)).toHaveText(/^Failed/);
  await shot(page, "03-run-failed.png");
});

test("03 the ready-to-merge park, its merge readiness answering ready", async ({ page }) => {
  const run = await runOf(page, "DEMO-9");
  await page.route(/\/runs\/\d+\/merge$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: body.ticket === "DEMO-9" ? { ...body, ready: true, reason: null, detail: null } : body });
  });
  await open(page, run);
  await expect(verdict(page)).toHaveText(/is ready for your merge\.$/);
  await shot(page, "04-run-ready.png");
});

test("04 a run number the daemon does not hold", async ({ page }) => {
  await open(page, 999);
  await expect(page.getByText("demo has no run 999.")).toBeVisible();
  await shot(page, "05-run-not-found.png");
});

test("05 the header at 390 px wraps its facts line and scrolls nothing sideways", async ({ page }) => {
  const run = await runOf(page, "DEMO-7");
  await open(page, run, 390);
  await expect(verdict(page)).toHaveText(/^Reviewing/);
  const wide = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(wide).toBe(false);
  await page.locator("[data-run-facts]").scrollIntoViewIfNeeded();
  await shot(page, "06-run-390.png");
});
