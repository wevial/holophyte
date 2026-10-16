import path from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function openTicket(browser: Browser, baseURL: string | undefined, key: string) {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  const address = new URL(baseURL ?? "http://127.0.0.1").host;
  await context.addInitScript(([key, token]) => localStorage.setItem(key, token), [`holophyte.token.${address}`, process.env.CONSOLE_TOKEN ?? ""]);
  const page = await context.newPage();
  await page.goto(`/next.html#/tickets/demo/${key}`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(actions(page).getByRole("button", { name: "Cancel ticket" })).toBeVisible();
  return { context, page };
}

const actions = (page: Page) => page.locator("[data-ticket-actions]");

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

test.describe.configure({ mode: "serial" });

test("the seeded ready DEMO-2 with Move to backlog and Cancel ticket, at dawn and night", async ({ browser, baseURL }) => {
  const { context, page } = await openTicket(browser, baseURL, "DEMO-2");
  await expect(actions(page).getByRole("button", { name: "Move to backlog" })).toBeVisible();
  await shot(page, "01-moves-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-moves-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await context.close();
});

test("the cancel dialog on the live DEMO-3 with a reason typed", async ({ browser, baseURL }) => {
  const { context, page } = await openTicket(browser, baseURL, "DEMO-3");
  await actions(page).getByRole("button", { name: "Cancel ticket" }).click();
  const dialog = page.getByRole("dialog", { name: "Cancel DEMO-3?" });
  await expect(dialog).toContainText("stops now");
  await dialog.getByRole("textbox", { name: "Reason · saved with the ticket" }).fill("Wrong scope: the ledger pages already.");
  await shot(page, "03-cancel-dialog.png");
  await dialog.getByRole("button", { name: "Keep it" }).click();
  await context.close();
});

test("Requeue on the backlog draft DEMO-1, refused with its problems", async ({ browser, baseURL }) => {
  const { context, page } = await openTicket(browser, baseURL, "DEMO-1");
  await actions(page).getByRole("button", { name: "Requeue" }).click();
  await expect(actions(page).getByRole("alert")).toBeVisible();
  await expect(actions(page).getByRole("alert")).not.toContainText("answered");
  await page.mouse.move(0, 0);
  await shot(page, "04-moves-refused.png");
  await context.close();
});

test("the toast after Move to backlog on DEMO-2", async ({ browser, baseURL }) => {
  const { context, page } = await openTicket(browser, baseURL, "DEMO-2");
  await actions(page).getByRole("button", { name: "Move to backlog" }).click();
  await expect(page.locator("[data-toast]")).toHaveText("Moved DEMO-2 to Backlog.");
  await expect(actions(page).getByRole("button", { name: "Requeue" })).toBeVisible();
  await page.mouse.move(0, 0);
  await shot(page, "05-moves-toast.png");
  await context.close();
});
