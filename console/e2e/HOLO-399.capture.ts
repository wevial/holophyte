import path from "node:path";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("review round limit, agent routes and capture settings", async ({ browser, baseURL }) => {
  const home = process.env.HOLOPHYTE_HOME!;
  const config = readdirSync(home, { recursive: true }).map(String).find((file) =>
    file.endsWith("config.toml") && readFileSync(path.join(home, file), "utf8").includes('name = "demo"'))!;
  const configPath = path.join(home, config);
  writeFileSync(configPath, readFileSync(configPath, "utf8").replace("[serve]", "[serve]\nconfig_edit = true") + `
[loop]
review_rounds = 2
review_rounds_max = 4
[review]
adversary = true
[agents]
implementer = "claude --model opus -p"
implementer_fallback = "codex exec"
reviewer = "codex --model gpt-6-astra exec"
adjudicator = "claude --model opus -p"
[merge]
ui_capture = ""
ui_paths = ["console/**", "site/**"]
ui_capture_video = false
`);
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 },
    colorScheme: "light", timezoneId: "UTC", recordVideo: { dir: out, size: { width: 1440, height: 1000 } } });
  const page = await context.newPage();
  await page.addInitScript(({ base, token }) => localStorage.setItem(`holophyte.token.${new URL(base).host}`, token),
    { base: baseURL!, token: process.env.CONSOLE_TOKEN! });
  await page.goto("/next.html#/settings/review?project=demo");
  await expect(page.getByRole("button", { name: "Single", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("group", { name: "Adversary", exact: true })).toContainText("On");
  await shot(page, "01-review-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-review-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  const cap = page.getByRole("group", { name: "Round cap", exact: true });
  for (let count = 0; count < 3; count++) await cap.getByRole("button").first().click();
  await expect(page.getByRole("alert")).toHaveText("Above the round cap of 1. Raise the cap or lower this.");
  await expect(page.getByRole("button", { name: "Review and save" })).toBeDisabled();
  await shot(page, "03-review-cap-error.png");
  await page.getByRole("button", { name: "Discard", exact: true }).click();
  await page.getByRole("link", { name: "Agents", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Implementer", exact: true })).toHaveValue("claude --model opus -p");
  await expect(page.getByRole("group", { name: "Implementer", exact: true })).toContainText("runs claude opus");
  await expect(page.getByRole("group", { name: "Reviewer", exact: true })).toContainText("runs codex gpt-6-astra");
  await expect(page.getByRole("group", { name: "Adjudicator", exact: true })).toContainText("runs claude opus");
  await shot(page, "04-agents.png");
  await page.getByRole("link", { name: "Capture", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "UI capture command", exact: true })).toHaveValue("");
  await expect(page.getByRole("switch", { name: "Video", exact: true })).toBeDisabled();
  await shot(page, "05-capture.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "settings-tabs-flow.webm"));
    await video.delete();
  }
});
