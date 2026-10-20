import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const WIDTH = 1600;
const HEIGHT = 900;

test.use({ viewport: { width: WIDTH, height: HEIGHT }, colorScheme: "light" });

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

const prs = (page: Page) => page.getByRole("region", { name: "Pull requests", exact: true });
const running = (page: Page) => page.getByRole("region", { name: "Running", exact: true });
const dayLog = (page: Page) => page.locator("aside[aria-labelledby='day-log']");

test("Pull requests waits on you, Running holds the fixing and On CI pull requests, and the day log fits the window", async ({ page }) => {
  await page.goto("/next.html#/");
  await expect(page.getByRole("button", { name: "Colour theme: dawn. Switch to night" })).toBeVisible();
  const rows = prs(page).locator("[data-pr-row]");
  await expect(rows.first()).toBeVisible();
  for (const wait of await rows.locator("[data-pr-wait]").allTextContents()) expect(wait).toBe("Your review");
  await expect(prs(page)).not.toContainText(/On CI|Factory fixing|waiting on the factory/);

  const fixing = running(page).locator("[data-fixing]");
  await expect(fixing.first()).toHaveText(/^fixing #\d+ · round \d+ of \d+$/);
  const onCi = running(page).locator("[data-on-ci]");
  await expect(onCi.first()).toContainText("checks running");

  const column = dayLog(page).locator("..");
  const box = await column.boundingBox();
  expect(box!.y + box!.height).toBeLessThanOrEqual(HEIGHT);
  await shot(page, "01-pr-waiting-dawn.png");

  const lane = running(page).getByRole("group").filter({ has: page.locator("[data-on-ci]") });
  await lane.scrollIntoViewIfNeeded();
  await page.evaluate(() => document.fonts.ready);
  await lane.screenshot({ path: path.join(out, "02-running-on-ci.png"), animations: "disabled" });

  await page.evaluate(() => window.scrollTo(0, 0));
  await dayLog(page).evaluate((node) => node.scrollTo(0, node.scrollHeight));
  const last = dayLog(page).locator("ol > li").last();
  const entry = await last.boundingBox();
  expect(entry!.y + entry!.height).toBeLessThanOrEqual(HEIGHT);
  const after = await column.boundingBox();
  expect(after!.y + after!.height).toBeLessThanOrEqual(HEIGHT);
  await shot(page, "03-day-log-fits.png");
});
