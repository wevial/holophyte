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
