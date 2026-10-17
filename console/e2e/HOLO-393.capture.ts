import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

test("Settings themes, host attention, folded rail and narrow layout", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/next.html#/settings/general");
  await expect(page.getByText("demo · config.toml on writer-host", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "General", exact: true })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-settings-dawn.png"), fullPage: true });
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await page.screenshot({ path: path.join(out, "02-settings-night.png"), fullPage: true });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await page.route("**/status", (route) => route.abort("connectionrefused"));
  await expect(page.getByRole("link", { name: "Settings, 1 host needs attention", exact: true })).toBeVisible({ timeout: 20000 });
  await page.screenshot({ path: path.join(out, "03-settings-footer-badge.png"), fullPage: true });
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await page.screenshot({ path: path.join(out, "04-settings-folded.png"), fullPage: true });
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("navigation", { name: "Settings sections" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(out, "05-settings-390.png"), fullPage: true });
});
