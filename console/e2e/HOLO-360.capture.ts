import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

async function openGraph(page: Page) {
  await page.goto("/next.html#/stories/demo/DEMO-12");
  await expect(page.getByRole("region", { name: "Story graph, scrolls sideways when wide" }).locator("[data-card]")).toHaveCount(6);
  await expect(page.locator('[data-edges="open"] path')).toHaveCount(4);
  return page.getByRole("region", { name: "Story graph, scrolls sideways when wide" });
}

test("01 DEMO-12's graph at 1440 by 900, dawn and night", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await openGraph(page);
  await expect(page.getByRole("list", { name: "Card states" })).toBeVisible();
  await shot(page, "01-graph-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-graph-night.png");
});

test("02 the graph at 1000 px, scrolled sideways to its last column", async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  const region = await openGraph(page);
  expect(await region.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
  await region.evaluate((el) => {
    el.scrollIntoView({ block: "center" });
    el.scrollLeft = el.scrollWidth;
  });
  await expect(page.locator('[data-node="DEMO-1"]')).toBeInViewport();
  await shot(page, "03-graph-wide.png");
});
