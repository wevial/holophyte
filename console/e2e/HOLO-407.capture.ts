import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  const style = await page.addStyleTag({ content: "main { overflow: visible !important; } #root > div { height: auto !important; overflow: visible !important; }" });
  await page.screenshot({ path: path.join(out, name), fullPage: true });
  await style.evaluate(element => element.remove());
}

test("structured ticket fields across themes, criteria movement and narrow layout", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
    timezoneId: "UTC",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } },
  });
  await context.addInitScript(({ base, token }) => {
    localStorage.setItem(`holophyte.token.${new URL(base).host}`, token);
  }, { base: baseURL!, token: process.env.CONSOLE_TOKEN! });
  const page = await context.newPage();
  await page.goto("/next.html#/tickets/demo/new");
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Steer notes show on the run detail card");
  await page.getByRole("textbox", { name: "Summary", exact: true }).fill("Show each steer note alongside its run, with the author and time.");
  await page.getByRole("textbox", { name: "What", exact: true }).fill("List steer notes on the run detail card.");
  await page.getByRole("textbox", { name: "Why", exact: true }).fill("Operators can see the guidance a run received.");
  await page.getByRole("button", { name: "+ Add a criterion", exact: true }).click();
  await page.getByRole("button", { name: "+ Add a criterion", exact: true }).click();
  await page.getByRole("textbox", { name: "AC1", exact: true }).fill("Each steer note shows its author and time.");
  await page.getByRole("textbox", { name: "AC2", exact: true }).fill("New notes appear without reloading the run.");
  await page.getByRole("textbox", { name: "AC3", exact: true }).fill("The list wraps without horizontal scrolling on mobile.");
  for (let count = 0; count < 4; count++) await page.getByRole("button", { name: "Increase estimate", exact: true }).click();
  await page.getByRole("textbox", { name: "Depends on", exact: true }).fill("demo-3");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByRole("button", { name: "Remove DEMO-3", exact: true })).toBeVisible();
  await page.getByRole("switch", { name: "This ticket changes the UI", exact: true }).click();
  await page.getByRole("textbox", { name: "Mock-up link", exact: true }).fill("https://claude.ai/artifact/abc");
  await page.getByRole("textbox", { name: "Boards", exact: true }).fill("https://example.test/board.png");
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "01-structured-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-structured-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await page.getByRole("button", { name: "Move AC3 up", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "AC2", exact: true })).toHaveValue("The list wraps without horizontal scrolling on mobile.");
  await expect(page.getByRole("button", { name: "Move AC1 up", exact: true })).toBeDisabled();
  await shot(page, "03-criteria-moved.png");
  await page.setViewportSize({ width: 390, height: 900 });
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "04-structured-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "structured-ticket-flow.webm"));
    await video.delete();
  }
});
