import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const TILE = path.join(import.meta.dirname, "..", "public", "icon-512.png");

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

const lane = (page: Page) => page.getByRole("region", { name: "Pull requests", exact: true });
const row = (page: Page, ticket: string) => lane(page).locator("[data-pr-row]").filter({ hasText: ticket });

async function showLane(page: Page) {
  await page.route(/^https:\/\/media\.example\//, (route) => route.fulfill({ path: TILE, contentType: "image/png" }));
  await page.goto("/next.html#/");
  await expect(row(page, "DEMO-9").locator("[data-facts]")).toContainText("6 rounds");
  await expect(row(page, "DEMO-9").locator("[data-facts]")).toContainText("waits on PR #41");
  await expect(row(page, "DEMO-13").locator("[data-pr-wait]")).toHaveText("Factory fixing");
  await expect(row(page, "DEMO-13").locator("[data-pr-since]")).toHaveText(/^round 4 · 1\dm$/);
  await lane(page).scrollIntoViewIfNeeded();
}

async function openDemo9(page: Page) {
  const demo9 = row(page, "DEMO-9");
  await demo9.getByRole("button", { name: /^PR #42, / }).click();
  await expect(demo9.locator("[data-pr-size]")).toHaveText("9 files, +412 −88");
  await expect(demo9.locator("[data-screenshot]")).toHaveCount(3);
  await expect(demo9.getByRole("link", { name: "Open preview" })).toBeVisible();
  for (const image of await demo9.locator("[data-screenshot] img").all()) {
    await image.scrollIntoViewIfNeeded();
    await expect.poll(() => image.evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth > 0)).toBe(true);
  }
  return demo9;
}

test("the pull request lane's rounds, waits on, Factory fixing and opened media", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  const page = await context.newPage();
  await showLane(page);
  await shot(page, "01-pr-lane-facts-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-pr-lane-facts-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  const demo9 = await openDemo9(page);
  await demo9.scrollIntoViewIfNeeded();
  await shot(page, "03-pr-row-media.png");
  await context.close();
});

test("the lane and the opened row at 390 px", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 390, height: 844 }, colorScheme: "light" });
  const page = await context.newPage();
  await showLane(page);
  await openDemo9(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.evaluate(() => document.fonts.ready);
  await lane(page).screenshot({ path: path.join(out, "04-pr-lane-facts-390.png"), animations: "disabled" });
  await context.close();
});
