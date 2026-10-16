import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const column = (page: Page, name: string) => page.getByRole("list", { name });
const lines = (page: Page, name: string) => column(page, name).locator("[data-state-line]");

test("the new console's Tickets board cards: time box, state glyph and line, in three looks", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/tickets");
  for (const name of ["Backlog", "Ready", "In flight", "Needs you", "Done today"]) {
    await expect(column(page, name).getByRole("article")).not.toHaveCount(0);
  }
  await expect(lines(page, "In flight").filter({ hasText: /round \d+ of \d+/ })).not.toHaveCount(0);
  await expect(lines(page, "In flight").filter({ hasText: /^on CI · PR #\d+$/ })).not.toHaveCount(0);
  await expect(lines(page, "Needs you").filter({ hasText: /^question open / })).not.toHaveCount(0);
  await expect(lines(page, "Needs you").filter({ hasText: /^failed · / })).not.toHaveCount(0);
  await expect(lines(page, "Done today").filter({ hasText: /^merged \d\d:\d\d$/ })).not.toHaveCount(0);
  await expect(column(page, "Backlog").getByRole("article").first()).toHaveAttribute("data-look", "backlog");
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-cards-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-cards-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");

  const flight = (await column(page, "In flight").boundingBox())!;
  const needs = (await column(page, "Needs you").boundingBox())!;
  const pad = 16;
  const bottom = Math.max(flight.y + flight.height, needs.y + needs.height);
  await page.screenshot({
    path: path.join(out, "03-cards-states.png"),
    clip: { x: flight.x - pad, y: flight.y - 56, width: needs.x + needs.width - flight.x + 2 * pad, height: bottom - flight.y + 56 + pad },
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(column(page, "Backlog").getByRole("article")).not.toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: path.join(out, "04-cards-390.png"), fullPage: true });
});
