import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const card = (page: Page, key: string) => page.locator(`a[id^="card-"][id$="-${key}"]`);
const peek = (page: Page) => page.getByRole("dialog");

async function openPeek(page: Page, key: string) {
  await page.goto("/next.html#/tickets");
  await expect(card(page, key)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await card(page, key).click();
  await expect(peek(page)).toBeVisible();
}

test("the ticket peek: the seeded DEMO-7's summary and criteria at dawn and night", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  const page = await context.newPage();
  await openPeek(page, "DEMO-7");
  await expect(peek(page).getByText("Add a CSV export endpoint for the orders list.")).toBeVisible();
  await expect(peek(page).locator("[data-met-count]")).toHaveText(/\d+ of \d+ met/);
  await expect(peek(page).locator("[data-standing]").first()).toBeVisible();
  await page.screenshot({ path: path.join(out, "01-peek-criteria-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(peek(page).locator("[data-met-count]")).toBeVisible();
  await page.screenshot({ path: path.join(out, "02-peek-criteria-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await context.close();
});

test("the ticket peek: a ticket whose /tickets/KEY answers 404 is not mirrored", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  const page = await context.newPage();
  await page.route("**/tickets/DEMO-7", (route) => route.fulfill({ status: 404, body: "not found" }));
  await openPeek(page, "DEMO-7");
  await expect(peek(page).getByText("This host has not mirrored DEMO-7.")).toBeVisible();
  await expect(peek(page).locator("[data-met-count]")).toHaveCount(0);
  await page.screenshot({ path: path.join(out, "03-peek-not-mirrored.png") });
  await context.close();
});
