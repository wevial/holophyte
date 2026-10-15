import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const merged = (page: Page) => page.getByRole("region", { name: "Merged today", exact: true });
const log = (page: Page) => page.getByRole("complementary", { name: "Today's log" });

async function shot(page: Page, name: string, fullPage = false) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage });
}

test("the new console's Overview: Merged today's rows and the day's log beside them, under them on a narrow window", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/");
  await expect(merged(page).getByRole("listitem").filter({ hasText: "DEMO-10" })).toBeVisible();
  await expect(log(page).getByRole("listitem").first()).toBeVisible();
  const column = await merged(page).boundingBox();
  const rail = await log(page).boundingBox();
  expect(rail!.x).toBeGreaterThan(column!.x + column!.width);
  await shot(page, "01-today-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-today-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await page.setViewportSize({ width: 1100, height: 900 });
  const narrowColumn = await merged(page).boundingBox();
  const narrowRail = await log(page).boundingBox();
  expect(narrowRail!.y).toBeGreaterThan(narrowColumn!.y + narrowColumn!.height);
  await log(page).scrollIntoViewIfNeeded();
  await shot(page, "03-today-1100.png");

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(log(page)).toBeAttached();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await shot(page, "04-today-390.png", true);
});
