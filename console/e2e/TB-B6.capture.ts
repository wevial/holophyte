import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const ready = (run: number) => ({
  run, ticket: "DEMO-9", pr_url: "https://github.com/example/demo/pull/42",
  head_sha: "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f", ready: true, reason: null, detail: null,
  facts: [
    { name: "parked", ok: true, detail: `run ${run} is DEMO-9's newest run, parked awaiting_merge_approval` },
    { name: "human_approval", ok: true, detail: "[merge] approve is \"human\"" },
    { name: "review_approved", ok: true, detail: "GitHub's review decision is APPROVED" },
    { name: "checks_passed", ok: true, detail: "the required checks are success" },
    { name: "mergeable", ok: true, detail: "GitHub's mergeable is MERGEABLE" },
    { name: "head_unchanged", ok: true, detail: "origin's branch and the pull request are at 5acc138" },
  ],
});

const card = (page: Page, key: string) => page.locator(`a[id^="card-"][id$="-${key}"]`);
const peek = (page: Page) => page.getByRole("dialog");
const ask = (page: Page) => peek(page).locator("[data-peek-ask]");

async function openPeek(page: Page, key: string) {
  await page.route("**/runs/*/merge", (route) => {
    const run = Number(new URL(route.request().url()).pathname.split("/").at(-2));
    return route.fulfill({ json: ready(run) });
  });
  await page.route("**/actions/requeue", (route) => route.fulfill({ json: { ok: true, detail: "requeued" } }));
  await page.goto("/next.html#/tickets");
  await expect(card(page, key)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await card(page, key).click();
  await expect(ask(page)).toBeVisible();
}

test("the ticket peek: the failed DEMO-11 asks first with Requeue, then says it was requeued", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  const page = await context.newPage();
  await openPeek(page, "DEMO-11");
  await expect(ask(page).getByRole("button")).toHaveText(["Requeue"]);
  await page.screenshot({ path: path.join(out, "01-peek-ask-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-peek-ask-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");

  await ask(page).getByRole("button", { name: "Requeue" }).click();
  await expect(peek(page).locator("[data-peek-acted]")).toHaveText("Requeued DEMO-11.");
  await expect(ask(page)).toHaveCount(0);
  await page.screenshot({ path: path.join(out, "03-peek-acted.png") });
  await context.close();
});

test("the ticket peek: the ready park DEMO-9 asks first with Merge", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  const page = await context.newPage();
  await openPeek(page, "DEMO-9");
  await expect(ask(page).getByRole("button")).toHaveText(["Merge"]);
  await page.screenshot({ path: path.join(out, "04-peek-merge.png") });
  await context.close();
});
