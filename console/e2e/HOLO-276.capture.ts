import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

test("the new console's shell: sidebar, rail, narrow layout and a scoped route", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html");
  const pages = page.getByRole("navigation", { name: "Pages" });
  await expect(pages.getByRole("link", { name: /^Overview, \d+ need you$/ })).toHaveAttribute("aria-current", "page");
  await expect(page.getByText(/ · polled /)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-shell-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-shell-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await expect(page.getByRole("button", { name: "Expand sidebar" })).toHaveAttribute("aria-expanded", "false");
  await page.screenshot({ path: path.join(out, "03-shell-folded.png") });
  await page.getByRole("button", { name: "Expand sidebar" }).click();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("complementary", { name: "Console" })).toHaveCSS("width", "390px");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: path.join(out, "04-shell-390.png"), fullPage: true });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/next.html#/runs?project=demo");
  await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
  await expect(pages.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
  await page.screenshot({ path: path.join(out, "05-runs-scoped.png") });
});
