import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

const graphRegion = (page: Page) => page.getByRole("region", { name: "Story graph, scrolls sideways when wide" });

async function openStory(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/stories/demo/DEMO-12");
  await expect(page.locator("[data-card]")).toHaveCount(12);
  await expect(page.getByRole("heading", { name: /Decisions and witnesses/ })).toBeVisible();
}

test("01 DEMO-12's graph and its Decisions and witnesses at 1440 by 900, dawn and night", async ({ page }) => {
  await openStory(page, 1440);
  await expect(graphRegion(page)).toBeVisible();
  await expect(page.locator("[data-step]").first()).toBeHidden();
  const notes = page.getByRole("heading", { name: /Decisions and witnesses/ });
  await notes.evaluate((el) => el.scrollIntoView({ block: "end" }));
  await shot(page, "01-notes-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-notes-night.png");
});

test("02 the page at 390 px, the step list in place of the graph and nothing scrolling sideways", async ({ page }) => {
  await openStory(page, 390);
  await expect(graphRegion(page)).toBeHidden();
  await expect(page.getByRole("region", { name: "Step 1" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("region", { name: "Step 1" }).evaluate((el) => el.scrollIntoView({ block: "start" }));
  await shot(page, "03-steps-390.png");
});

test("03 the graph at a 1024 px viewport, its right edge faded before scrolling", async ({ page }) => {
  await openStory(page, 1024);
  const region = graphRegion(page);
  await expect(region).toBeVisible();
  expect(await region.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
  await expect(region).toHaveClass(/fade-r/);
  await region.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await shot(page, "04-graph-fade.png");
});
