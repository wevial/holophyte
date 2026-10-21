import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

test("a Running lane holds its live runs and its pull request on CI in one box", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1600, height: 900 }, colorScheme: "light", timezoneId: "UTC" });
  await context.addInitScript(({ base, token }) => {
    localStorage.setItem(`holophyte.token.${new URL(base).host}`, token);
  }, { base: baseURL!, token: process.env.CONSOLE_TOKEN! });
  const page = await context.newPage();
  await page.goto("/next.html#/");
  const lane = page.getByRole("region", { name: "Running" }).getByRole("group", { name: "demo" });
  const list = lane.getByRole("list", { name: "demo running" });
  await expect(list.locator("[data-on-ci]")).toHaveCount(1);
  await expect(list.locator("a[data-row]").first()).toBeVisible();
  await expect(list.locator("[data-on-ci-divider]")).toHaveText("On CI");
  await lane.scrollIntoViewIfNeeded();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-on-ci-in-lane.png"), animations: "disabled" });
  await context.close();
});
