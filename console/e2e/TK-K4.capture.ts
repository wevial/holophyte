import path from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const NOTE = "Reuse the sweep's lease query instead of a second scan.";

async function openPage(browser: Browser, baseURL: string | undefined, width = 1440, height = 900) {
  const context = await browser.newContext({ baseURL, viewport: { width, height }, colorScheme: "light" });
  return { context, page: await context.newPage() };
}

/** `/actions/steer` answered with `answer`; an `ok` one also adds the note
 *  to the ticket's next `/tickets/KEY` read, as the daemon would. */
async function intercept(page: Page, answer: { ok: boolean; detail: string }) {
  const taken: { note: string; hint: boolean }[] = [];
  await page.route("**/actions/steer", async (route) => {
    const body = route.request().postDataJSON() as { note: string; hint: boolean };
    if (answer.ok) taken.push(body);
    await route.fulfill({ json: { action: "steer", ticket: "DEMO-7", ...answer } });
  });
  await page.route("**/tickets/DEMO-7", async (route) => {
    const response = await route.fetch();
    const ticket = await response.json();
    const added = taken.map((each, index) => ({
      id: 9000 + index, kind: each.hint ? "hint" : "amendment", note: each.note, author: "maintainer",
      at: Date.now(), run: null, read_by: null, read_ms: null, withdrawn: false,
    }));
    await route.fulfill({ response, json: { ...ticket, steers: [...ticket.steers, ...added] } });
  });
}

async function openBox(page: Page) {
  await page.goto("/next.html#/tickets/demo/DEMO-7");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.getByRole("button", { name: "Steer", exact: true }).click();
  await expect(box(page)).toBeVisible();
}

const box = (page: Page) => page.getByRole("region", { name: "Steer · amend this ticket" });

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

test("the Steer box on the seeded DEMO-7, open on This run with a note typed, at dawn and night", async ({ browser, baseURL }) => {
  const { context, page } = await openPage(browser, baseURL);
  await intercept(page, { ok: true, detail: "steered" });
  await openBox(page);
  await box(page).getByRole("button", { name: "This run" }).click();
  await box(page).getByRole("textbox", { name: "Steer note" }).fill(NOTE);
  await shot(page, "01-steer-box-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-steer-box-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await context.close();
});

test("the notes list after an amendment is saved", async ({ browser, baseURL }) => {
  const { context, page } = await openPage(browser, baseURL);
  await intercept(page, { ok: true, detail: "steered" });
  await openBox(page);
  await box(page).getByRole("textbox", { name: "Steer note" }).fill("Add a criterion: a stale lease is requeued once, never twice.");
  await box(page).getByRole("button", { name: "Save amendment" }).click();
  await expect(box(page)).toBeHidden();
  const list = page.getByRole("region", { name: /^Steer notes and amendments/ });
  await expect(list.getByText("a stale lease is requeued once", { exact: false })).toBeVisible();
  await list.scrollIntoViewIfNeeded();
  await shot(page, "03-steer-notes.png");
  await context.close();
});

test("the Steer box showing the daemon's refusal", async ({ browser, baseURL }) => {
  const { context, page } = await openPage(browser, baseURL);
  await intercept(page, { ok: false, detail: "DEMO-7 is merged; nothing to steer" });
  await openBox(page);
  await box(page).getByRole("textbox", { name: "Steer note" }).fill("Add a criterion for the empty board.");
  await box(page).getByRole("button", { name: "Save amendment" }).click();
  await expect(box(page).getByRole("alert")).toHaveText("DEMO-7 is merged; nothing to steer");
  await shot(page, "04-steer-refused.png");
  await context.close();
});

test("the Steer box at 390 px", async ({ browser, baseURL }) => {
  const { context, page } = await openPage(browser, baseURL, 390, 844);
  await intercept(page, { ok: true, detail: "steered" });
  await openBox(page);
  await box(page).getByRole("button", { name: "This run" }).click();
  await box(page).getByRole("textbox", { name: "Steer note" }).fill(NOTE);
  await box(page).scrollIntoViewIfNeeded();
  await shot(page, "05-steer-390.png");
  await context.close();
});
