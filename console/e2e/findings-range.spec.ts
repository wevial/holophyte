import { expect, test } from "@playwright/test";

for (const textSize of [110, 120]) {
  test(`at 375px and ${textSize}% text, Custom range opens its dates menu inside the viewport`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.addInitScript((size) => localStorage.setItem("holophyte.textSize", size), String(textSize));
    await page.goto("/#/findings?from=2026-10-01&to=2026-10-15");
    await page.getByRole("button", { name: "Custom range" }).click();
    const menu = page.getByRole("group", { name: "Range range" });
    await expect(menu.getByLabel("From")).toHaveValue("2026-10-01");
    const box = (await menu.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
    await expect(page.getByText("Something went wrong")).toHaveCount(0);
  });
}

test("at 320px and 120% text with a custom range picked, the range presets and Custom range wrap to stay inside the viewport", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 812 });
  await page.addInitScript(() => localStorage.setItem("holophyte.textSize", "120"));
  await page.goto("/#/findings?from=2026-10-01&to=2026-10-15");
  const custom = page.getByRole("button", { name: "Custom range" });
  await expect(custom).toHaveText("Oct 1 – Oct 15");
  for (const button of [...(await page.getByRole("group", { name: "Range" }).getByRole("button").all()), custom]) {
    const box = (await button.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(320);
  }
});

test("at 720x425 and 125% text, expanding the folded sidebar by shortcut with Custom range open keeps the dates menu and Done inside the viewport", async ({ page }) => {
  await page.setViewportSize({ width: 720, height: 425 });
  await page.addInitScript(() => {
    localStorage.setItem("holophyte.textSize", "125");
    localStorage.setItem("holophyte.next.rail", "folded");
  });
  await page.goto("/#/findings");
  await page.getByRole("button", { name: "Custom range" }).click();
  const menu = page.getByRole("group", { name: "Range range" });
  await expect(menu).toBeVisible();
  await page.keyboard.press("Control+Backslash");
  await expect(page.getByRole("button", { name: "Collapse sidebar" })).toBeVisible();
  await expect(async () => {
    for (const part of [menu, menu.getByRole("button", { name: "Done" })]) {
      const box = (await part.boundingBox())!;
      expect([box.x >= 0, box.y >= 0, box.x + box.width <= 720, box.y + box.height <= 425]).toEqual([true, true, true, true]);
    }
  }).toPass({ timeout: 2000 });
});
