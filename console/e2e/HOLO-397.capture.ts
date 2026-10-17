import path from "node:path";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

test("General settings and pending worker change across themes and widths", async ({ page }) => {
  const home = process.env.HOLOPHYTE_HOME!;
  const config = readdirSync(home, { recursive: true }).map(String).find((file) => file.endsWith("config.toml") && readFileSync(path.join(home, file), "utf8").includes('name = "demo"'))!;
  const configPath = path.join(home, config);
  writeFileSync(configPath, readFileSync(configPath, "utf8").replace("[serve]", "[serve]\nconfig_edit = true") + '\n[merge]\nmode = "pr"\napprove = "auto"\n[loop]\nworkers = 2\n');
  await page.addInitScript(({ base, token }) => localStorage.setItem(`holophyte.token.${new URL(base).host}`, token), { base: process.env.CONSOLE_URL!, token: process.env.CONSOLE_TOKEN! });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/next.html#/settings/general");
  await expect(page.getByRole("button", { name: "One more worker" })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-general-dawn.png"), fullPage: true });
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await page.screenshot({ path: path.join(out, "02-general-night.png"), fullPage: true });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await page.getByRole("button", { name: "One more worker" }).click();
  await expect(page.getByRole("region", { name: "Unsaved changes" })).toContainText("1 unsaved change");
  await page.screenshot({ path: path.join(out, "03-general-changed.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(out, "04-general-390.png"), fullPage: true });
});
