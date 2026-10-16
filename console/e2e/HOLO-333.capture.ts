import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const MINUTE = 60_000;

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

const runs = (page: Page) => page.getByRole("list", { name: "Runs, newest first" }).getByRole("link");

async function open(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/runs");
  await expect(runs(page).first()).toBeVisible();
}

/** The seeded pull request park for DEMO-9 answers ready to merge. */
async function readyDemo9(page: Page) {
  await page.route(/\/runs\/\d+\/merge$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: body.ticket === "DEMO-9" ? { ...body, ready: true, reason: null, detail: null } : body });
  });
}

test("01 stage words and a tinted ready-to-merge row, night and dawn", async ({ page }) => {
  await readyDemo9(page);
  await open(page);
  const ready = runs(page).filter({ has: page.getByRole("img", { name: "ready to merge" }) }).first();
  await expect(ready).toHaveAttribute("data-need", "");
  await expect(runs(page).filter({ hasText: /merged \d\d:\d\d/ }).first()).toBeVisible();
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-runs-words-night.png");

  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await shot(page, "02-runs-words-dawn.png");
});

test("02 a stale run's row, its heartbeat aged past the threshold", async ({ page }) => {
  let staleId: number | null = null;
  let staleAge = 0;
  await page.route(/\/status$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (!Array.isArray(body.runs) || !body.thresholds) return route.fulfill({ response });
    staleAge = body.thresholds.heartbeat_stale_ms + 15 * MINUTE;
    const live = body.runs.find((run: { id: number }) => staleId == null || run.id === staleId);
    if (live) {
      staleId = live.id;
      live.heartbeat_age_ms = staleAge;
    }
    await route.fulfill({ response, json: body });
  });
  await page.route(/\/history\?/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    for (const row of body.rows) if (row.id === staleId) row.heartbeat_age_ms = staleAge;
    await route.fulfill({ response, json: body });
  });
  await open(page);
  const stale = runs(page).filter({ hasText: /stale · no beat \d+m/ }).first();
  await expect(stale).toHaveAttribute("data-need", "");
  await shot(page, "03-runs-stale.png");
});
