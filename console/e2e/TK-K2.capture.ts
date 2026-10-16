import path from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function openPage(browser: Browser, baseURL: string | undefined, key: string, width = 1440, height = 900) {
  const context = await browser.newContext({ baseURL, viewport: { width, height }, colorScheme: "light" });
  const page = await context.newPage();
  return { context, page, ready: () => goTo(page, key) };
}

async function goTo(page: Page, key: string) {
  await page.goto(`/next.html#/tickets/demo/${key}`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator("[data-met-count]")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

test("the ticket page: the seeded DEMO-7 in review at dawn and night", async ({ browser, baseURL }) => {
  const { context, page, ready } = await openPage(browser, baseURL, "DEMO-7");
  await ready();
  await expect(page.getByRole("status", { name: "Board" })).toBeVisible();
  await page.screenshot({ path: path.join(out, "01-ticket-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-ticket-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await context.close();
});

test("the ticket page: the seeded ready DEMO-2", async ({ browser, baseURL }) => {
  const { context, page, ready } = await openPage(browser, baseURL, "DEMO-2");
  await ready();
  await expect(page.getByRole("status", { name: "Board" })).toContainText("Ready");
  await page.screenshot({ path: path.join(out, "03-ticket-ready.png") });
  await context.close();
});

test("the ticket page: the Verify fold open", async ({ browser, baseURL }) => {
  const { context, page, ready } = await openPage(browser, baseURL, "DEMO-7");
  await ready();
  await page.locator("summary", { hasText: "Verify" }).click();
  await expect(page.locator("details pre")).toBeVisible();
  await page.screenshot({ path: path.join(out, "04-ticket-verify-open.png") });
  await context.close();
});

test("the ticket page: a ticket no host has mirrored", async ({ browser, baseURL }) => {
  const { context, page } = await openPage(browser, baseURL, "DEMO-7");
  await page.route("**/tickets/DEMO-7", (route) => route.fulfill({ status: 404, body: "not found" }));
  await page.goto("/next.html#/tickets/demo/DEMO-7");
  await expect(page.getByText("No host of demo has mirrored DEMO-7.")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "05-ticket-not-mirrored.png") });
  await context.close();
});

test("the ticket page at 390 px: the facts under the main column", async ({ browser, baseURL }) => {
  const { context, page, ready } = await openPage(browser, baseURL, "DEMO-7", 390, 844);
  await ready();
  await expect(page.getByRole("complementary", { name: "Ticket facts and story" })).toBeVisible();
  await page.screenshot({ path: path.join(out, "06-ticket-390.png"), fullPage: true });
  await context.close();
});
