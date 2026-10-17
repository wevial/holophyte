import path from "node:path";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("new ticket across themes, monospace commands, Tickets entry, refused filing and narrow layout", async ({ browser, baseURL }) => {
  const home = process.env.HOLOPHYTE_HOME!;
  const config = readdirSync(home, { recursive: true }).map(String).find(file =>
    file.endsWith("config.toml") && readFileSync(path.join(home, file), "utf8").includes('name = "demo"'))!;
  const configPath = path.join(home, config);
  const original = readFileSync(configPath, "utf8");
  const setting = 'private_patterns = ["capture-private-marker"]\n';
  writeFileSync(configPath, original.includes("[merge]")
    ? original.replace("[merge]", `[merge]\n${setting}`)
    : `${original}\n[merge]\n${setting}`);
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } },
  });
  await context.addInitScript(({ base, token }) => {
    localStorage.setItem(`holophyte.token.${new URL(base).host}`, token);
  }, { base: baseURL!, token: process.env.CONSOLE_TOKEN! });
  const page = await context.newPage();
  await page.goto("/next.html#/tickets/demo/new");
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Steer notes show on the run detail card");
  await page.getByRole("textbox", { name: "Summary", exact: true }).fill("The run page lists each steer note.");
  await expect(page.getByRole("textbox", { name: "Verify commands", exact: true })).toHaveCSS("font-family", /IBM Plex Mono/);
  await shot(page, "01-new-ticket-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-new-ticket-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await page.goto("/next.html#/tickets?project=demo");
  await expect(page.getByRole("button", { name: "New ticket", exact: true })).toBeVisible();
  await shot(page, "03-tickets-button.png");
  await page.getByRole("button", { name: "New ticket", exact: true }).click();
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Steer notes show on the run detail card");
  await page.getByRole("textbox", { name: "Summary", exact: true }).fill("The run page lists each steer note.");
  await page.getByRole("textbox", { name: "Implementation notes", exact: true }).fill("capture-private-marker");
  const refusal = page.waitForResponse(response => response.url().endsWith("/tickets") && response.request().method() === "POST");
  await page.getByRole("button", { name: "File ticket", exact: true }).click();
  const response = await refusal;
  expect(response.status()).toBe(422);
  const answer = await response.json();
  expect(answer.problems.length).toBeGreaterThan(0);
  for (const problem of answer.problems) await expect(page.getByText(problem, { exact: true })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "04-new-ticket-refused.png");
  await page.setViewportSize({ width: 390, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "05-new-ticket-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "new-ticket-flow.webm"));
    await video.delete();
  }
});
