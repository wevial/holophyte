import path from "node:path";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("Raw TOML checks, marks and reviews a whole-file edit across themes and widths", async ({ browser, baseURL }) => {
  const home = process.env.HOLOPHYTE_HOME!;
  const config = readdirSync(home, { recursive: true }).map(String).find(file => file.endsWith("config.toml") && readFileSync(path.join(home, file), "utf8").includes('name = "demo"'))!;
  const configPath = path.join(home, config);
  const original = readFileSync(configPath, "utf8").replace("[serve]", "[serve]\nconfig_edit = true") + '\n[merge]\nmode = "pr"\napprove = "auto"\n[loop]\nworkers = 2\n';
  writeFileSync(configPath, original);
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1050 }, colorScheme: "light",
    recordVideo: { dir: out, size: { width: 1440, height: 1050 } } });
  const page = await context.newPage();
  await page.addInitScript(({ base, token }) => localStorage.setItem(`holophyte.token.${new URL(base).host}`, token),
    { base: baseURL!, token: process.env.CONSOLE_TOKEN! });
  await page.goto("/next.html#/settings/toml?project=demo");
  const editor = page.getByRole("textbox", { name: "config.toml for demo" });
  await expect(editor).toBeVisible();
  const text = await editor.inputValue();
  await expect(page.getByText("no errors · ready to save")).toBeVisible();
  await shot(page, "01-toml-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-toml-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await editor.fill(text.replace("workers = 2", "workers = 0"));
  await expect(page.getByText("1 error blocks saving")).toBeVisible();
  await expect(page.locator('[data-refused="true"]')).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
  await editor.evaluate(node => { node.scrollTop = node.scrollHeight; node.dispatchEvent(new Event("scroll")); });
  await shot(page, "03-toml-refused.png");
  await editor.fill(text.replace("workers = 2", "workers = 3"));
  await expect(page.getByText("no errors · ready to save")).toBeVisible();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const confirm = page.getByRole("dialog", { name: "Save config.toml?" });
  await expect(confirm).toContainText("workers = 2");
  await expect(confirm).toContainText("workers = 3");
  await shot(page, "04-toml-confirm.png");
  await confirm.getByRole("button", { name: "Keep editing" }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await editor.fill(text.replace("workers = 2", "workers = 3") + '# This whole-file editor keeps long configuration lines inside its own scrolling box.\n');
  await expect(page.getByText("no errors · ready to save")).toBeVisible();
  expect(await editor.evaluate(node => node.scrollWidth > node.clientWidth)).toBe(true);
  await editor.evaluate(node => { node.scrollTop = node.scrollHeight; node.scrollLeft = 90; node.dispatchEvent(new Event("scroll")); });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "05-toml-390.png");
  const video = page.video();
  await context.close();
  if (video) { await video.saveAs(path.join(out, "raw-toml-flow.webm")); await video.delete(); }
});
