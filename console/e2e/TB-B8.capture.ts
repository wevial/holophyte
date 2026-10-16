import path from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const row = (page: Page, key: string) => page.locator(`[data-lane-rows] a[id^="card-"][id$="-${key}"]`);

async function table(browser: Browser, baseURL: string | undefined, width: number, height = 900) {
  const context = await browser.newContext({ baseURL, viewport: { width, height }, colorScheme: "light" });
  const page = await context.newPage();
  await page.goto("/next.html#/tickets");
  await page.getByRole("group", { name: "View" }).getByRole("button", { name: "Table" }).click();
  await expect(page.getByRole("button", { name: "Table", pressed: true })).toBeVisible();
  await expect(row(page, "DEMO-3")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  return { context, page };
}

test("the Tickets table at dawn and night, Group by None", async ({ browser, baseURL }) => {
  const { context, page } = await table(browser, baseURL, 1440);
  await expect(page.getByRole("button", { name: "None", pressed: true })).toBeVisible();
  await expect(page.locator("[data-table-heads]")).toBeVisible();
  await page.screenshot({ path: path.join(out, "01-table-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-table-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await context.close();
});

test("the Tickets table grouped by Project", async ({ browser, baseURL }) => {
  const { context, page } = await table(browser, baseURL, 1440);
  await page.getByRole("group", { name: "Group by" }).getByRole("button", { name: "Project" }).click();
  await expect(page.getByRole("region", { name: "demo" }).getByRole("heading", { level: 2 })).toBeVisible();
  await page.screenshot({ path: path.join(out, "03-table-lanes.png") });
  await context.close();
});

test("a Tickets table row open in the peek", async ({ browser, baseURL }) => {
  const { context, page } = await table(browser, baseURL, 1440);
  await row(page, "DEMO-3").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(row(page, "DEMO-3")).toHaveAttribute("data-open", "true");
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(out, "04-table-peek.png") });
  await context.close();
});

test("the Tickets table at 390 px", async ({ browser, baseURL }) => {
  const { context, page } = await table(browser, baseURL, 390, 844);
  await expect(page.locator("[data-table-heads]")).toBeHidden();
  await expect(row(page, "DEMO-3").locator("[data-cell=stage]")).toBeHidden();
  await page.screenshot({ path: path.join(out, "05-table-390.png"), fullPage: true });
  await context.close();
});
