import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  const style = await page.addStyleTag({ content: "main { overflow: visible !important; } #root > div { height: auto !important; overflow: visible !important; }" });
  await page.screenshot({ path: path.join(out, name), fullPage: true });
  await style.evaluate(element => element.remove());
}

test("the checker marks errors as the ticket is typed and releases File ticket once none is left", async ({ browser, baseURL }) => {
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
  const checker = page.getByRole("region", { name: "Checker", exact: true });
  await expect(checker.getByText("Nothing to check yet. Start with a title.", { exact: true })).toBeVisible();
  const fill = (name: string, value: string) => page.getByRole("textbox", { name, exact: true }).fill(value);
  await fill("Title", "Steer notes show on the run detail card");
  await fill("What", "List steer notes on the run detail card.");
  await fill("How", "Read the run's notes through the existing notes query.");
  await fill("In scope", "The run detail card");
  await fill("Out of scope", "Editing notes");
  await page.getByRole("button", { name: "+ Add a criterion", exact: true }).click();
  await fill("AC1", "Given a run with two steer notes, when its page opens, then both notes show with author and time.");
  await fill("Verify commands", "ruff check .");
  await expect(checker.getByText(/errors? blocks? filing/)).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Summary", exact: true })).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("textbox", { name: "AC2", exact: true })).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("button", { name: "File ticket", exact: true })).toBeDisabled();
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "01-checker-errors-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-checker-errors-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await fill("Summary", "The run page lists each steer note beside the run it steered.");
  await fill("AC2", "Given a new steer note, when it is sent, then it shows on the run page without a reload.");
  await expect(checker.getByText("Ready. Nothing to fix.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "File ticket", exact: true })).toBeEnabled();
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "03-checker-ready.png");

  await fill("Summary", "");
  await expect(checker.getByText(/errors? blocks? filing/)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 900 });
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "04-checker-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "checker-flow.webm"));
    await video.delete();
  }
});
