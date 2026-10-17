import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}
test("browser appearance follows choices through settings, overview and run", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/next.html#/settings/appearance");
  await page.getByRole("radio", { name: "Dawn", exact: true }).check();
  await shot(page, "01-appearance-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.getByRole("radio", { name: "Night", exact: true })).toBeChecked();
  await shot(page, "02-appearance-night.png");
  await page.getByRole("radio", { name: "Plex Serif", exact: true }).check();
  await page.getByRole("link", { name: /^Overview/ }).click();
  await expect(page.getByRole("article", { name: "Focus", exact: true })).toBeVisible();
  await expect(page.locator(".font-read").first()).toHaveCSS("font-family", /^"IBM Plex Serif"/);
  await shot(page, "03-appearance-serif.png");
  await page.goto("/next.html#/settings/appearance");
  await page.getByRole("radio", { name: "Hyperlegible", exact: true }).check();
  const response = await page.request.get("/projects/demo/history?since=0");
  const { rows } = await response.json();
  const run = Math.max(...rows.filter((row: { ticket: string }) => row.ticket === "DEMO-7").map((row: { id: number }) => row.id));
  await page.route("**/runs/*/asks", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, asks: [{
      id: 1, question: "What did verification find?", author: "maintainer",
      asked_ms: Date.now() - 60000, answered_ms: Date.now(), url: null,
      answer: "The sweep frees a silent lease. Verification passed; the worker route is ready for review.",
    }] } });
  });
  await page.goto(`/next.html#/runs/demo/${run}`);
  await expect(page.locator("#agent")).toBeVisible();
  await expect(page.locator("#agent li.font-read").last()).toBeVisible();
  await expect(page.locator("#agent li.font-read").last()).toHaveCSS("font-family", /^"Atkinson Hyperlegible Next"/);
  await shot(page, "04-appearance-hyperlegible.png");
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto("/next.html#/settings/appearance");
  await expect(page.getByRole("radio", { name: "Hyperlegible", exact: true })).toBeChecked();
  await shot(page, "05-appearance-390.png");
});
