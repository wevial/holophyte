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

async function open(page: Page, run: number) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`/next.html#/runs/demo/${run}`);
  await expect(progress(page).locator("li").first()).toBeVisible();
}

async function toProgress(page: Page) {
  await page.getByRole("link", { name: "history in Progress" }).click();
}

test("01 the reviewing run's Progress log with its live mark, night and dawn", async ({ page }) => {
  const run = await runOf(page, "DEMO-7");
  await open(page, run);
  await expect(progress(page).locator("[data-live]")).toHaveText("live");
  await toProgress(page);
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-progress-live-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await shot(page, "02-progress-live-dawn.png");
});

test("02 a steer line and its read-at sub-line", async ({ page }) => {
  const run = await runOf(page, "DEMO-7");
  await page.route(new RegExp(`/runs/${run}$`), async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const first = Math.min(...body.events.map((event: { at: number }) => event.at));
    const at = first + 4 * 60_000;
    const note = {
      id: 9001, kind: "amendment", note: "don't add a new flag for the dry run; reuse --act", author: "operator",
      at, run, consumed_by: run, consumed_ms: at + 60_000, interrupt: false, withdrawn: false,
    };
    await route.fulfill({ response, json: { ...body, steer_notes: [...body.steer_notes, note] } });
  });
  await open(page, run);
  const steer = progress(page).locator('li[data-kind="steer"]').first();
  await expect(steer).toContainText("You steered: don't add a new flag for the dry run; reuse --act");
  await expect(steer).toContainText(/→ the agent read it at \d\d:\d\d/);
  await toProgress(page);
  await shot(page, "03-progress-steer.png");
});

test("03 the stranded failure's log ends on its failure line", async ({ page }) => {
  const run = await runOf(page, "DEMO-11");
  await open(page, run);
  await expect(progress(page).locator("[data-live]")).toHaveCount(0);
  await expect(progress(page).locator("li").last()).toHaveAttribute("data-kind", "failure");
  await toProgress(page);
  await shot(page, "04-progress-failed.png");
});
