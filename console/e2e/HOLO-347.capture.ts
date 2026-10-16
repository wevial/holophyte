import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string, fullPage = true) {
  await page.mouse.move(1, 1);
  await page.waitForTimeout(500);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage });
}

async function ready(page: Page) {
  await expect(page.locator("[data-count-line]")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("[data-finding-row]").first()).toBeVisible();
}

const rowOf = (page: Page, group: string, text: string) =>
  page.getByRole("region", { name: group }).locator("[data-finding-row]").filter({ hasText: text }).first();

async function unfold(page: Page, group: string, text: string) {
  const button = rowOf(page, group, text).locator("button[aria-expanded]");
  await button.click();
  await expect(button).toHaveAttribute("aria-expanded", "true");
}

async function unfoldDemo7(page: Page) {
  const open = page.getByRole("region", { name: "Open" }).locator("[data-finding-row]");
  const count = await open.count();
  for (let index = 0; index < count; index += 1) {
    const button = open.nth(index).locator("button[aria-expanded]");
    await button.click();
    if (await open.nth(index).locator("[data-place]").textContent().then((text) => text?.includes("DEMO-7"))) return;
    await button.click();
  }
  throw new Error("no open row of DEMO-7");
}

test("01 every group listing its rows, DEMO-7's open row unfolded, Dawn and Night", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/findings");
  await ready(page);
  await unfoldDemo7(page);
  await shot(page, "01-rows-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-rows-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
});

test("02 Follow-ups with DEMO-7's pending feature and DEMO-5's ledger row unfolded", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/findings");
  await ready(page);
  await unfold(page, "Follow-ups", "Show the shadow implementer");
  await unfold(page, "Follow-ups", "Fail the export test");
  await page.getByRole("region", { name: "Follow-ups" }).scrollIntoViewIfNeeded();
  await shot(page, "03-rows-followups.png", false);
});

test("03 the page at 390 px with a row unfolded, nothing scrolling sideways", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/findings");
  await ready(page);
  await unfoldDemo7(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await shot(page, "04-rows-390.png");
});
