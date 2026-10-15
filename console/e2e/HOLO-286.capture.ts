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

async function intercept(page: Page, { actions = true } = {}) {
  await page.route("**/runs/*/merge", (route) => {
    const run = Number(new URL(route.request().url()).pathname.split("/").at(-2));
    return route.fulfill({ json: ready(run) });
  });
  await page.route("**/actions/requeue", (route) => route.fulfill({ json: { ok: true, detail: "requeued" } }));
  if (!actions) {
    await page.route("**/status", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, actions: false } });
    });
  }
}

const card = (page: Page) => page.getByRole("article", { name: "Focus" });

async function show(page: Page, key: string) {
  const row = page.getByRole("button", { name: `Show ${key}`, exact: true });
  if (await row.count()) await row.click();
  await expect(card(page)).toHaveAttribute("data-focus", key);
}

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

async function open(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/");
  await expect(card(page)).toBeVisible();
}

test("the focus card acts: Requeue with More open, the Merge confirm, the toast, and a host with actions off", async ({ page }) => {
  await intercept(page);
  await open(page);

  await show(page, "DEMO-11");
  await expect(card(page).getByRole("button", { name: "Requeue" })).toBeVisible();
  await card(page).getByRole("button", { name: "More" }).click();
  await expect(card(page).getByRole("group", { name: "More actions" })).toBeVisible();
  await shot(page, "01-actions-failed-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(card(page).getByRole("group", { name: "More actions" })).toBeVisible();
  await shot(page, "02-actions-failed-night.png");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await show(page, "DEMO-9");
  await card(page).getByRole("button", { name: "Merge" }).click();
  const dialog = page.getByRole("dialog", { name: "Merge PR #42?" });
  await expect(dialog).toContainText("GitHub's review decision is APPROVED");
  await shot(page, "03-merge-dialog.png");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  await show(page, "DEMO-11");
  await card(page).getByRole("button", { name: "Requeue" }).click();
  await expect(page.locator("[data-toast]")).toHaveText("Requeued DEMO-11.");
  await shot(page, "04-toast.png");
});

test("a host with actions off draws the card's links and says how to turn actions on", async ({ page }) => {
  await intercept(page, { actions: false });
  await open(page);
  await show(page, "DEMO-11");
  await expect(card(page)).toContainText("Actions are off on this host. [serve] actions = true turns them on.");
  await expect(card(page).getByRole("link", { name: "Open run" })).toBeVisible();
  await shot(page, "05-actions-off.png");
});
