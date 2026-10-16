import path from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function openPage(browser: Browser, baseURL: string | undefined, width = 1440, height = 900) {
  const context = await browser.newContext({ baseURL, viewport: { width, height }, colorScheme: "light" });
  return { context, page: await context.newPage() };
}

async function goTo(page: Page, key: string) {
  await page.goto(`/next.html#/tickets/demo/${key}`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator("[data-ticket-runs] tbody tr").first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

test("the ticket page's Runs: the seeded DEMO-11, failed and still in flight, at dawn and night", async ({ browser, baseURL }) => {
  const { context, page } = await openPage(browser, baseURL);
  await goTo(page, "DEMO-11");
  await page.screenshot({ path: path.join(out, "01-ticket-runs-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-ticket-runs-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await context.close();
});

test("the ticket page's Story: DEMO-7, a child of the seeded story DEMO-12", async ({ browser, baseURL }) => {
  const { context, page } = await openPage(browser, baseURL);
  await goTo(page, "DEMO-7");
  const story = page.getByRole("region", { name: "Story" });
  await expect(story.getByText("this ticket", { exact: false })).toBeVisible();
  await page.screenshot({ path: path.join(out, "03-ticket-story.png") });
  await context.close();
});

test("the ticket page's Runs at 390 px, scrolling inside its box", async ({ browser, baseURL }) => {
  const { context, page } = await openPage(browser, baseURL, 390, 844);
  await goTo(page, "DEMO-11");
  const box = page.locator("[data-ticket-runs]");
  await box.evaluate((element) => element.scrollBy({ left: 60 }));
  await box.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, "04-ticket-runs-390.png") });
  await context.close();
});
