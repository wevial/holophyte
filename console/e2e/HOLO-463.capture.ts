import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const MINUTE = 60_000;

test("01 the demo lane draws its running witness set under its live run, in Dawn", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.route("**/projects/demo/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const elapsed = 4 * MINUTE;
    const witnesses = { story: "DEMO-20", keys: ["W3", "W4", "W5", "W6"], started_ms: body.now - elapsed, elapsed_ms: elapsed };
    await route.fulfill({ response, json: { ...body, runs: body.runs.slice(1, 2), workers: 2, witnesses } });
  });
  await page.goto("/next.html#/");
  await expect(page.getByRole("button", { name: "Colour theme: dawn. Switch to night" })).toBeVisible();
  const running = page.getByRole("region", { name: "Running", exact: true });
  const demo = running.getByRole("group", { name: "demo" });
  await expect(demo.locator("[data-witnesses]")).toHaveText("Witnesses · DEMO-20 · W3–W6 · running 4m");
  await expect(demo.getByRole("link", { name: /^DEMO-\d+ / })).toHaveCount(1);
  await expect(demo.locator("[data-lane-head]")).toContainText("1 of 2 workers busy");
  await running.scrollIntoViewIfNeeded();
  await page.evaluate(() => document.fonts.ready);
  await running.screenshot({ path: path.join(out, "01-running-witnesses.png"), animations: "disabled" });
});
