import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

for (const [index, theme] of ["dark", "light"].entries()) {
  const name = theme === "dark" ? "night" : "dawn";
  test(`${name} reading text, titles and log`, async ({ page }) => {
    await page.route("**/attention", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      for (const item of body.items ?? []) {
        if (item.ticket === "DEMO-11") item.title = "Retry the board sync";
      }
      await route.fulfill({ response, json: body });
    });
    await page.emulateMedia({ colorScheme: "light" });
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.goto("/next.html#/stories/demo/DEMO-12");
    if (theme === "dark") {
      await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
      await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    }
    await expect(page.locator("[data-card]")).toHaveCount(12);
    await expect(page.getByRole("heading", { name: /Decisions and witnesses/ })).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(out, `${String(index * 3 + 1).padStart(2, "0")}-story-${name}.png`), fullPage: true });

    await page.goto("/next.html#/");
    await expect(page.getByRole("list", { name: "Then", exact: true })).toBeVisible();
    await expect(page.getByRole("list", { name: "Then", exact: true }).getByText("Retry the board sync")).toBeVisible();
    await expect(page.locator("aside[aria-labelledby='day-log'] ol")).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(out, `${String(index * 3 + 2).padStart(2, "0")}-overview-${name}.png`), fullPage: true });

    await page.goto("/next.html#/tickets");
    await expect(page.locator("h3").first()).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(out, `${String(index * 3 + 3).padStart(2, "0")}-tickets-${name}.png`), fullPage: true });
  });
}
