import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const TILE = path.join(import.meta.dirname, "..", "public", "icon-512.png");

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

const lane = (page: Page) => page.getByRole("region", { name: "Pull requests", exact: true });
const rows = (page: Page) => lane(page).locator("[data-pr-row]");
const row = (page: Page, ticket: string) => rows(page).filter({ hasText: ticket });

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

async function showLane(page: Page) {
  await page.route(/^https:\/\/media\.example\//, (route) => route.fulfill({ path: TILE, contentType: "image/png" }));
  await page.goto("/next.html#/");
  await expect(row(page, "DEMO-9").locator("[data-facts] [data-fact]").last()).toHaveText("9 files +412 −88");
  await expect(row(page, "DEMO-13").locator("[data-pr-wait]")).toHaveText("Factory fixing");
  await lane(page).scrollIntoViewIfNeeded();
}

async function openDemo9(page: Page) {
  const demo9 = row(page, "DEMO-9");
  await demo9.getByRole("button", { name: /^PR #42, / }).click();
  await expect(demo9).toHaveAttribute("data-open", "true");
  await expect(rows(page).and(page.locator("[data-open]"))).toHaveCount(1);
  await expect(demo9.getByRole("link", { name: "Open Vercel preview" })).toBeVisible();
  for (const image of await demo9.locator("[data-screenshot] img").all()) {
    await image.scrollIntoViewIfNeeded();
    await expect.poll(() => image.evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth > 0)).toBe(true);
  }
  return demo9;
}

async function measured(page: Page) {
  const count = await rows(page).count();
  expect(count).toBeGreaterThan(0);
  return Promise.all(
    Array.from({ length: count }, async (_, index) => {
      const each = rows(page).nth(index);
      return {
        title: await box(each.locator("[data-pr-title]")),
        facts: await box(each.locator("[data-row] [data-facts]")),
        status: await box(each.locator("[data-pr-state]")),
      };
    }),
  );
}

test("the pull request lane drawn as its mock, opened, in both themes", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  const page = await context.newPage();
  await showLane(page);
  const demo9 = await openDemo9(page);
  await lane(page).scrollIntoViewIfNeeded();
  await demo9.scrollIntoViewIfNeeded();
  await shot(page, "01-pr-lane-mock-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-pr-lane-mock-night.png");
  await context.close();
});

test("the lane keeps three columns at 760 px", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 760, height: 900 }, colorScheme: "light" });
  const page = await context.newPage();
  await showLane(page);
  for (const { title, facts, status } of await measured(page)) {
    expect(status.x).toBeGreaterThan(title.x + title.width);
    expect(status.y).toBeLessThan(facts.y + facts.height);
  }
  await shot(page, "03-pr-lane-760.png");
  await context.close();
});

test("the lane and the opened row stack at 390 px", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 390, height: 844 }, colorScheme: "light" });
  const page = await context.newPage();
  await showLane(page);
  await openDemo9(page);
  for (const { facts, status } of await measured(page)) {
    expect(status.y).toBeGreaterThan(facts.y + facts.height);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.evaluate(() => document.fonts.ready);
  await lane(page).screenshot({ path: path.join(out, "04-pr-lane-390.png"), animations: "disabled" });
  await context.close();
});
