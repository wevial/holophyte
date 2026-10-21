import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const toggleOf = (row: Locator) => row.locator(":scope > div > button");

async function tabTo(page: Page, target: Locator) {
  for (let step = 0; step < 60; step += 1) {
    if (await target.evaluate(element => element === document.activeElement)) return;
    await page.keyboard.press("Tab");
  }
  throw new Error("Tab never reached the row's toggle");
}

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

test("Waiting on you rows open in place from the keyboard", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/next.html#/settings/appearance");
  await page.getByRole("radio", { name: "Dawn", exact: true }).check();
  await page.getByRole("link", { name: /^Overview/ }).click();
  const section = page.getByRole("region", { name: "Waiting on you", exact: true });
  const rows = section.locator("li[data-wait-row]");
  await expect(rows.nth(1)).toBeVisible();
  await expect(section.getByText("1 of", { exact: false })).toHaveCount(0);
  await expect(section.getByRole("button", { name: "Later, show the next one" })).toHaveCount(0);

  const first = toggleOf(rows.nth(0));
  const second = toggleOf(rows.nth(1));
  await expect(first).toHaveAttribute("aria-expanded", "true");
  await expect(second).toHaveAttribute("aria-expanded", "false");
  await first.focus();
  await tabTo(page, second);
  await page.keyboard.press("Enter");
  await expect(second).toHaveAttribute("aria-expanded", "true");
  await expect(first).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Space");
  await expect(second).toHaveAttribute("aria-expanded", "false");
  await expect(section.locator("li[data-wait-row][data-open]")).toHaveCount(0);

  const question = rows.filter({ has: page.getByRole("img", { name: "question", exact: true }) }).first();
  await toggleOf(question).click();
  await expect(toggleOf(question)).toHaveAttribute("aria-expanded", "true");
  await expect(question).toHaveAttribute("data-open", "true");
  const detail = page.locator(`[id="${await toggleOf(question).getAttribute("aria-controls")}"]`);
  await expect(detail.getByRole("heading", { level: 4 })).toBeVisible();
  await page.mouse.move(0, 0);
  await shot(page, "01-waiting-on-you-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(toggleOf(question)).toHaveAttribute("aria-expanded", "true");
  await shot(page, "02-waiting-on-you-night.png");
});
