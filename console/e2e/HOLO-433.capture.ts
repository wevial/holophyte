import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

const lane = (page: Page) => page.getByRole("region", { name: "Pull requests", exact: true });
const row = (page: Page, ticket: string) => lane(page).locator("[data-pr-row]").filter({ hasText: ticket });

async function showLane(page: Page) {
  await page.route(/\/runs\/\d+\/merge$/, async (route) => {
    const response = await route.fetch();
    await route.fulfill({ status: 200, json: { ...(await response.json()), ready: true, reason: null, detail: null } });
  });
  await page.goto("/next.html#/");
  await expect(row(page, "DEMO-8").locator("[data-pr-wait]")).toHaveText("On CI");
  await expect(row(page, "DEMO-9").locator("[data-pr-wait]")).toHaveText("Your review");
  await lane(page).scrollIntoViewIfNeeded();
}

test("the pull request lane, its opened row and Send back", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  await showLane(page);
  await expect(row(page, "DEMO-8").locator("[data-pr-since]")).toContainText("checks running");
  await expect(row(page, "DEMO-9").locator("[data-pr-since]")).toContainText("ready to merge");
  await shot(page, "01-pr-lane-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-pr-lane-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  const demo9 = row(page, "DEMO-9");
  await demo9.getByRole("button", { name: /^PR #42, / }).click();
  await expect(demo9.locator("[data-pr-detail]")).toBeVisible();
  await expect(demo9.getByRole("link", { name: "Open PR to review" })).toBeVisible();
  await expect(demo9.getByRole("button", { name: "Send back with a note" })).toBeVisible();
  await expect(demo9.getByRole("link", { name: "Open run" })).toBeVisible();
  await demo9.scrollIntoViewIfNeeded();
  await shot(page, "03-pr-row-open.png");

  await demo9.getByRole("button", { name: "Send back with a note" }).click();
  await demo9.getByLabel("Note for the factory").fill("Rebase on main first, then rerun the e2e checks.");
  await demo9.getByRole("button", { name: "Send", exact: true }).scrollIntoViewIfNeeded();
  await shot(page, "04-pr-row-send-back.png");

  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "pr-row-flow.webm"));
    await video.delete();
  }
});

test("the pull request lane at 390 px", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 390, height: 844 }, colorScheme: "light" });
  const page = await context.newPage();
  await showLane(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await shot(page, "05-pr-lane-390.png");
  await context.close();
});
