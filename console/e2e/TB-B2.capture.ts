import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function emptied(page: Page) {
  await page.route("**/board", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const columns = body.columns.map((column: object) => ({ ...column, tickets: [] }));
    await route.fulfill({ response, json: { ...body, columns } });
  });
  await page.route("**/shipped?*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, rows: [], next_before: null } });
  });
}

const heading = (page: Page) => page.getByRole("heading", { level: 1, name: "Tickets" });
const countLine = (page: Page) => page.locator("[data-count-line]");

test("the new console's Tickets page: header, count line and five columns", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/tickets");
  await expect(heading(page)).toBeVisible();
  await expect(countLine(page)).toHaveText(/^all projects · \d+ tickets? \/ \d+ in flight \/ \d+ need you \/ \d+ ready$/);
  for (const name of ["Backlog", "Ready", "In flight", "Needs you", "Done today"]) {
    await expect(page.getByRole("list", { name })).toBeAttached();
  }
  await expect(page.getByRole("list", { name: "Done today" }).getByRole("listitem")).not.toHaveCount(0);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-tickets-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-tickets-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await page.goto("/next.html#/tickets?project=demo");
  await expect(countLine(page)).toHaveText(/^demo · /);
  await expect(page.getByText("Showing demo only")).toBeVisible();
  await expect(page.getByRole("button", { name: "Show all projects" })).toBeVisible();
  await page.screenshot({ path: path.join(out, "03-tickets-scoped.png") });

  const empty = await page.context().newPage();
  await emptied(empty);
  await empty.goto("/next.html#/tickets");
  await expect(empty.getByText("No open tickets.")).toBeVisible();
  await expect(countLine(empty)).toHaveText("all projects · 0 tickets / 0 in flight / 0 need you / 0 ready");
  await empty.evaluate(() => document.fonts.ready);
  await empty.screenshot({ path: path.join(out, "04-tickets-empty.png") });
  await empty.close();

  await page.goto("/next.html#/tickets");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(countLine(page)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  const box = page.locator("[data-board-scroll]");
  expect(await box.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await box.evaluate((element) => element.scrollTo({ left: 300 }));
  await page.screenshot({ path: path.join(out, "05-tickets-390.png"), fullPage: true });
});
