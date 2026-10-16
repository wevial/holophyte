import path from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const lane = (page: Page, name: string | RegExp) => page.getByRole("region", { name });
const laneHead = (page: Page, name: string | RegExp) => lane(page, name).getByRole("heading", { level: 2 }).getByRole("button");

async function grouped(browser: Browser, baseURL: string | undefined, width: number, by: "Project" | "Story", setup?: (page: Page) => Promise<void>) {
  const context = await browser.newContext({ baseURL, viewport: { width, height: 900 }, colorScheme: "light" });
  const page = await context.newPage();
  await setup?.(page);
  await page.goto("/next.html#/tickets");
  await page.getByRole("group", { name: "Group by" }).getByRole("button", { name: by }).click();
  await expect(page.getByRole("button", { name: by, pressed: true })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  return { context, page };
}

async function oneStory(page: Page) {
  await page.route("**/board", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const keys = body.columns.flatMap((column: { tickets: { ticket: string }[] }) => column.tickets.map((ticket) => ticket.ticket));
    const story = { ticket: "DEMO-20", title: "Export the orders list", children: keys.slice(0, 3), merged: 0, total: 3 };
    await route.fulfill({ response, json: { ...body, stories: [story] } });
  });
}

test("the Tickets board grouped by Project at dawn and night", async ({ browser, baseURL }) => {
  const { context, page } = await grouped(browser, baseURL, 1440, "Project");
  await expect(laneHead(page, "demo")).toHaveAttribute("aria-expanded", "true");
  await expect(lane(page, "demo").locator("[data-lane-meta]")).toHaveText(/^\d+ tickets?$/);
  await page.screenshot({ path: path.join(out, "01-lanes-project-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-lanes-project-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await context.close();
});

test("the Tickets board grouped by Story: one story lane and No story", async ({ browser, baseURL }) => {
  const { context, page } = await grouped(browser, baseURL, 1440, "Story", oneStory);
  await expect(lane(page, /^DEMO-20 · /)).toBeVisible();
  await expect(lane(page, /^DEMO-20 · /).locator("[data-lane-meta]")).toHaveText(/^3 tickets · \d+ done today$/);
  await expect(lane(page, "No story")).toBeVisible();
  await page.screenshot({ path: path.join(out, "03-lanes-story.png") });

  await laneHead(page, /^DEMO-20 · /).click();
  await expect(laneHead(page, /^DEMO-20 · /)).toHaveAttribute("aria-expanded", "false");
  await expect(laneHead(page, "No story")).toHaveAttribute("aria-expanded", "true");
  await page.screenshot({ path: path.join(out, "04-lanes-folded.png") });
  await context.close();
});

test("the Tickets board's lanes at 390 px", async ({ browser, baseURL }) => {
  const { context, page } = await grouped(browser, baseURL, 390, "Story", oneStory);
  await expect(lane(page, "No story")).toBeVisible();
  await page.screenshot({ path: path.join(out, "05-lanes-390.png") });
  await context.close();
});
