import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const card = (page: Page, key: string) => page.locator(`a[id^="card-"][id$="-${key}"]`);
const peek = (page: Page) => page.getByRole("dialog");
const runFact = (page: Page) => peek(page).locator("dt", { hasText: "Run" }).locator("xpath=following-sibling::dd[1]");

test("the new console's Tickets board peek: a card opens its ticket at the right edge", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } } });
  const page = await context.newPage();
  await page.goto("/next.html#/tickets");
  await expect(card(page, "DEMO-3")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);

  await card(page, "DEMO-3").click();
  await expect(peek(page)).toBeVisible();
  await expect(peek(page).getByRole("button", { name: "Close preview" })).toBeFocused();
  await expect(card(page, "DEMO-3")).toHaveAttribute("data-open", "true");
  await expect(runFact(page)).toContainText(/run \d+/);
  await expect(runFact(page)).toContainText("implementing");
  await page.screenshot({ path: path.join(out, "01-peek-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(peek(page)).toBeVisible();
  await page.screenshot({ path: path.join(out, "02-peek-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");

  await card(page, "DEMO-2").click();
  await expect(peek(page)).toHaveCount(1);
  await expect(peek(page).locator("[data-peek-key]")).toHaveText("DEMO-2");
  await expect(peek(page).locator("[data-peek-column]")).toHaveText("Ready");
  await expect(runFact(page)).toHaveText("none yet");
  await expect(card(page, "DEMO-3")).not.toHaveAttribute("data-open");
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(out, "03-peek-ready.png") });

  await page.keyboard.press("Escape");
  await expect(peek(page)).toHaveCount(0);
  await expect(card(page, "DEMO-2")).toBeFocused();
  await page.waitForTimeout(400);

  await page.setViewportSize({ width: 390, height: 844 });
  await card(page, "DEMO-3").click();
  await expect(peek(page)).toBeVisible();
  const box = (await peek(page).boundingBox())!;
  expect(box.x).toBe(0);
  expect(box.width).toBe(390);
  await page.screenshot({ path: path.join(out, "04-peek-390.png") });
  await page.waitForTimeout(400);

  const video = page.video();
  await context.close();
  if (video) await video.saveAs(path.join(out, "peek-flow.webm"));
  if (video) await video.delete();
});
