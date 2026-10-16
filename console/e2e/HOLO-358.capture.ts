import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

async function openStory(page: Page, key = "DEMO-12") {
  await page.goto(`/next.html#/stories/demo/${key}`);
}

test("01 the story DEMO-12 at 1440 by 900, dawn and night", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await openStory(page);
  await expect(page.locator("[data-story-chip]")).toHaveText("Needs you");
  await expect(page.locator("[data-merged]")).toHaveText(/\d+ of 6 merged/);
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toHaveText("demo tickets / Stories / DEMO-12");
  await shot(page, "01-story-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-story-night.png");
});

test("02 a story the daemon does not serve", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await openStory(page, "DEMO-99");
  await expect(page.getByRole("heading", { name: "No story DEMO-99 in demo.", level: 1 })).toBeVisible();
  await shot(page, "03-story-missing.png");
});

test("03 the story at 390 px, nothing scrolling sideways", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "light" });
  await openStory(page);
  await expect(page.locator("[data-story-chip]")).toHaveText("Needs you");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("navigation", { name: "Breadcrumb" }).evaluate((crumb) => crumb.scrollIntoView({ block: "start" }));
  await shot(page, "04-story-390.png");
});
