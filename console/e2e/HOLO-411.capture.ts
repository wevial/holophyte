import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const TICKET = "/projects/demo/tickets/DEMO-2";
const AC2 = "Given an export of no orders, when GET /orders.csv, then only the header line.";
const THEIR_AC2 = "Given an export of no orders, when GET /orders.csv, then only the header line, with a 200.";
const MY_AC2 = "Given an export of no orders, when GET /orders.csv, then the header line and an empty-export note.";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  const style = await page.addStyleTag({ content: "main { overflow: visible !important; } #root > div { height: auto !important; overflow: visible !important; }" });
  await page.screenshot({ path: path.join(out, name), fullPage: true });
  await style.evaluate(element => element.remove());
}

/** DEMO-2 saved through the daemon as `edit` makes of its current body. */
async function saveThroughDaemon(request: APIRequestContext, edit: (body: string) => string) {
  const headers = { authorization: `Bearer ${process.env.CONSOLE_TOKEN!}` };
  const read = await request.get(TICKET, { headers });
  expect(read.ok()).toBe(true);
  const ticket = await read.json() as { body: string; current: { revision: number; body: string } };
  const body = edit(ticket.current.body);
  const saved = await request.put(TICKET, { headers: { ...headers, "if-match": String(ticket.current.revision) }, data: { body } });
  expect(saved.status(), await saved.text()).toBe(200);
}

test("DEMO-2 saved elsewhere during an edit opens the conflict banner, and mine goes on top of revision 4", async ({ browser, baseURL }) => {
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
  const request = context.request;
  await saveThroughDaemon(request, body => body.replace(/^(- \[ \] .*)$/m, `$1\n- [ ] ${AC2}`));
  await saveThroughDaemon(request, body => body.replace(/^(## Summary\s*\n\s*\n)(.*)$/m, "$1$2 It also covers an empty export."));
  const page = await context.newPage();

  await page.goto("/next.html#/tickets/demo/DEMO-2/edit");
  await expect(page.getByRole("heading", { name: "Edit DEMO-2", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save revision 4", exact: true })).toBeDisabled();
  await page.getByRole("textbox", { name: "AC2", exact: true }).fill(MY_AC2);
  await saveThroughDaemon(request, body => body.replace(AC2, THEIR_AC2));

  const banner = page.getByRole("alert", { name: "Newer revision" });
  await expect(banner.getByText("DEMO-2 changed while you were editing.", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(banner.getByText("AC2 · REVISION 4, THEIRS", { exact: true })).toBeVisible();
  await expect(banner.getByText(THEIR_AC2, { exact: true })).toBeVisible();
  await expect(banner.getByText(MY_AC2, { exact: true })).toBeVisible();
  await expect(page.getByText("revision 3 · newer revision 4 exists", { exact: true })).toBeVisible();
  await expect(page.getByText("Resolve the newer revision first. Your edit is safe.", { exact: true })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "01-conflict-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-conflict-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await page.setViewportSize({ width: 390, height: 900 });
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "04-conflict-390.png");
  await page.setViewportSize({ width: 1440, height: 900 });

  await banner.getByRole("button", { name: "Put mine on top of revision 4", exact: true }).click();
  await expect(banner).toHaveCount(0);
  await expect(page.getByText("Revision 4 loaded with your edits on top. Review, then save as revision 5.", { exact: true })).toBeVisible();
  await expect(page.getByText("revision 4 + your edit", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save revision 5", exact: true })).toBeEnabled();
  await expect(page.getByRole("textbox", { name: "AC2", exact: true })).toHaveValue(MY_AC2);
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "03-conflict-on-top.png");

  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "conflict-flow.webm"));
    await video.delete();
  }
});
