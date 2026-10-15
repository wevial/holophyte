import path from "node:path";
import { expect, test } from "@playwright/test";

test("new console themes and classic palette", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html");
  await expect(page.getByRole("heading", { name: "Holophyte" })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(236, 234, 243)");
  await expect(page.locator("body")).toHaveCSS("font-family", 'Archivo, "Helvetica Neue", Arial, sans-serif');
  await page.screenshot({ path: path.join(process.env.CAPTURE_OUT!, "01-next-dawn.png") });
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(5, 7, 15)");
  await page.screenshot({ path: path.join(process.env.CAPTURE_OUT!, "02-next-night.png") });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Board", exact: true })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(244, 241, 234)");
  await page.screenshot({ path: path.join(process.env.CAPTURE_OUT!, "03-classic-unchanged.png") });
});
