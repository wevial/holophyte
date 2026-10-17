import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  const style = await page.addStyleTag({ content: "main { overflow: visible !important; } #root > div { height: auto !important; overflow: visible !important; }" });
  await page.screenshot({ path: path.join(out, name), fullPage: true });
  await style.evaluate(element => element.remove());
}

test("DEMO-2 is edited from its Ticket page: a changed criterion is marked, an emptied Summary holds Save", async ({ browser, baseURL }) => {
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

  await page.goto("/next.html#/tickets/demo/DEMO-2");
  const actions = page.locator("[data-ticket-actions]");
  const edit = actions.getByRole("link", { name: "Edit", exact: true });
  await expect(edit).toBeVisible();
  await expect(actions.getByRole("button", { name: "Move to backlog", exact: true })).toBeVisible();

  await edit.click();
  await expect(page).toHaveURL(/#\/tickets\/demo\/DEMO-2\/edit$/);
  await expect(page.getByRole("heading", { name: "Edit DEMO-2", exact: true })).toBeVisible();
  const checker = page.getByRole("region", { name: "Checker", exact: true });
  const save = page.getByRole("button", { name: "Save revision 2", exact: true });
  await expect(save).toBeDisabled();
  await page.getByRole("button", { name: "+ Add a criterion", exact: true }).click();
  await page.getByRole("textbox", { name: "AC2", exact: true }).fill("Given an export of no orders, when GET /orders.csv, then only the header line.");
  await page.getByRole("textbox", { name: "Verify commands", exact: true }).fill("ruff check .");
  await expect(page.getByRole("textbox", { name: "AC2", exact: true }).locator("xpath=ancestor::*[@data-changed][1]")).toBeVisible();
  await expect(checker.getByText("Ready. Nothing to fix.", { exact: true })).toBeVisible();
  await expect(page.getByText("2 changes · Acceptance criteria, Verify commands · the next run of DEMO-2 reads the new text", { exact: true })).toBeVisible();
  await expect(save).toBeEnabled();
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "01-edit-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-edit-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await page.goto("/next.html#/tickets/demo/DEMO-2");
  await expect(actions.getByRole("link", { name: "Edit", exact: true })).toBeVisible();
  await shot(page, "03-ticket-edit-button.png");

  await page.goto("/next.html#/tickets/demo/DEMO-2/edit");
  const summary = page.getByRole("textbox", { name: "Summary", exact: true });
  await summary.fill("");
  await expect(checker.getByText(/errors? blocks? saving/)).toBeVisible();
  await expect(summary).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("button", { name: "Save revision 2", exact: true })).toBeDisabled();
  await expect(page.getByText(/^Fix \d+ errors? to save\.$/)).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "04-edit-blocked.png");

  await page.getByRole("button", { name: "Discard", exact: true }).click();
  await page.getByRole("button", { name: "+ Add a criterion", exact: true }).click();
  await page.getByRole("textbox", { name: "AC2", exact: true }).fill("Given an export of no orders, when GET /orders.csv, then only the header line.");
  await page.getByRole("textbox", { name: "Verify commands", exact: true }).fill("ruff check .");
  await expect(checker.getByText("Ready. Nothing to fix.", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 900 });
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "05-edit-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "edit-flow.webm"));
    await video.delete();
  }
});
