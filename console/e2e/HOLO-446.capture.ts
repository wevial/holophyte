import path from "node:path";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("Raw TOML colours the file by kind and keeps gutter numbers level with their scrolled lines while PageDown moves the selection", async ({ page, baseURL }) => {
  const home = process.env.HOLOPHYTE_HOME!;
  const config = readdirSync(home, { recursive: true }).map(String).find(file => file.endsWith("config.toml") && readFileSync(path.join(home, file), "utf8").includes('name = "demo"'))!;
  const configPath = path.join(home, config);
  const original = readFileSync(configPath, "utf8").replace("[serve]", "[serve]\nconfig_edit = true")
    + '\n# How candidates land on main\n[merge]\nmode = "pr"  # open a pull request\napprove = "auto"\nrequire_up_to_date = true\n\n[loop]\nworkers = 2\nstop_on_failure = true\n';
  writeFileSync(configPath, original);
  await page.addInitScript(({ base, token }) => localStorage.setItem(`holophyte.token.${new URL(base).host}`, token),
    { base: baseURL!, token: process.env.CONSOLE_TOKEN! });
  await page.goto("/next.html#/settings/toml?project=demo");
  const editor = page.getByRole("textbox", { name: "config.toml for demo" });
  await expect(editor).toBeVisible();
  const text = await editor.inputValue();
  await expect(page.getByText("no errors · ready to save")).toBeVisible();
  await expect(page.locator('pre [data-kind="table"]', { hasText: "[merge]" })).toBeVisible();
  await shot(page, "01-toml-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-toml-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await editor.fill(text.replace("workers = 2", "workers = 0"));
  await expect(page.getByText("1 error blocks saving")).toBeVisible();
  await expect(page.locator('[data-refused="true"]')).toHaveCount(1);
  await expect(page.locator("[data-refused-band]")).toHaveCount(1);
  await shot(page, "03-toml-refused.png");
  const head = text.trimEnd().split("\n");
  const long = [...head, ...Array.from({ length: 200 - head.length }, (_, index) => index % 3 === 0 ? `# note ${index}` : `# workers = ${index}`)].join("\n");
  await editor.fill(long);
  expect(long.split("\n")).toHaveLength(200);
  await expect(page.getByText("no errors · ready to save")).toBeVisible();
  const tops = await page.locator("[data-toml-scroller]").evaluate(scroller => {
    scroller.scrollTop = 1000;
    const gutter = scroller.querySelector('[data-line="60"]')!.getBoundingClientRect().top;
    const line = scroller.querySelector('pre [data-text-line="60"]')!.getBoundingClientRect().top;
    return { gutter, line, scrolled: scroller.scrollTop };
  });
  expect(tops.scrolled).toBe(1000);
  expect(Math.abs(tops.gutter - tops.line)).toBeLessThanOrEqual(1);
  await shot(page, "04-toml-scrolled.png");
  await editor.press("Control+Home");
  await editor.press("Shift+PageDown");
  const paged = await editor.evaluate((node: HTMLTextAreaElement) => [node.selectionStart, node.selectionEnd, node.closest("[data-toml-scroller]")!.scrollTop]);
  expect(paged[0]).toBe(0);
  expect(paged[1]).toBe(long.split("\n").slice(0, 16).join("\n").length + 1);
  expect(paged[2]).toBeGreaterThan(0);
});
