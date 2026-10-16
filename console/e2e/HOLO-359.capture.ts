import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

const stories = (page: Page) => page.getByRole("list", { name: "Stories" });

test("01 the sidebar's Stories list with DEMO-12 marked, dawn and night", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/stories/demo/DEMO-12");
  await expect(stories(page).getByRole("link", { name: /^DEMO-12 / })).toHaveAttribute("aria-current", "page");
  await expect(page.locator("[data-story-chip]")).toBeVisible();
  await shot(page, "01-stories-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-stories-night.png");
});

test("02 the story list folded under Tickets", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/stories/demo/DEMO-12");
  await expect(stories(page)).toBeVisible();
  await page.getByRole("button", { name: "Hide the story list" }).click();
  await expect(page.getByRole("button", { name: "Show the story list" })).toHaveAttribute("aria-expanded", "false");
  await expect(stories(page)).toHaveCount(0);
  await shot(page, "03-stories-folded.png");
});

test("03 #/stories with every board listing no stories", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.route("**/board", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), stories: [] } });
  });
  await page.goto("/next.html#/stories");
  await expect(page.getByRole("heading", { name: "No stories in all projects", level: 1 })).toBeVisible();
  await shot(page, "04-stories-none.png");
});
