import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("density choices persist through Overview and Runs", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/next.html#/settings/appearance");
  await page.getByRole("radio", { name: "Dawn", exact: true }).check();
  const comfortable = page.getByRole("button", { name: "Comfortable", exact: true });
  const compact = page.getByRole("button", { name: "Compact", exact: true });
  await expect(comfortable).toHaveAttribute("aria-pressed", "true");
  const preview = page.getByRole("region", { name: "Preview", exact: true });
  await expect(preview.locator("[data-row]")).toHaveCount(2);
  await expect(preview.locator("[data-row]").first()).toHaveCSS("min-height", "66px");
  await preview.scrollIntoViewIfNeeded();
  await shot(page, "01-density-comfortable.png");
  await compact.click();
  await expect(preview.locator("[data-row]").first()).toHaveCSS("min-height", "44px");
  await expect(preview.locator("[data-row]").first()).toHaveCSS("padding-block", "6px");
  await expect(compact).toHaveCSS("min-height", "44px");
  await shot(page, "02-density-compact.png");
  await page.getByRole("link", { name: /^Overview/ }).click();
  await expect(page.getByRole("article", { name: "Focus", exact: true })).toBeVisible();
  for (const button of await page.getByRole("button", { name: /^\d+ queued$/ }).all()) await button.click();
  await expect(page.locator("[data-row]").first()).toBeVisible();
  await page.getByRole("region", { name: "Running", exact: true }).evaluate((element) => element.scrollIntoView({ block: "start" }));
  await shot(page, "03-overview-compact-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "04-overview-compact-night.png");
  await page.goto("/next.html#/runs");
  await expect(page.locator("[data-run-table] [data-row]").first()).toBeVisible();
  await expect(page.locator("[data-run-table] [data-row]").first()).toHaveCSS("padding-block", "6px");
  await shot(page, "05-runs-compact.png");
});
