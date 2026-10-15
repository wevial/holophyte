import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const ready = (run: number) => ({
  run, ticket: "DEMO-9", pr_url: "https://github.com/example/demo/pull/42",
  head_sha: "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f", ready: true, reason: null, detail: null,
  facts: [{ name: "parked", ok: true, detail: `run ${run} is DEMO-9's newest run, parked awaiting_merge_approval` }],
});

const triage = { choice: "code", confidence: 0.81, backend: "claude", model: "triage-model", requeued: false };

async function intercept(page: Page) {
  await page.route("**/runs/*/merge", (route) => {
    const run = Number(new URL(route.request().url()).pathname.split("/").at(-2));
    return route.fulfill({ json: ready(run) });
  });
  await page.route("**/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const items = (body.items ?? []).map((item: { kind: string; ticket?: string }) =>
      item.kind === "failed" && item.ticket === "DEMO-11" ? { ...item, triage } : item);
    await route.fulfill({ response, json: { ...body, items } });
  });
}

const card = (page: Page) => page.getByRole("article", { name: "Focus" });

async function show(page: Page, key: string) {
  const row = page.getByRole("button", { name: `Show ${key}`, exact: true });
  if (await row.count()) await row.click();
  await expect(card(page)).toHaveAttribute("data-focus", key);
}

async function shot(page: Page, name: string, fullPage = false) {
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => null))));
  await page.screenshot({ path: path.join(out, name), fullPage });
}

test("the Needs you focus card: a failed run with triage, a ready PR, the rows under it and at 390 px", async ({ page }) => {
  await intercept(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/");
  await expect(card(page)).toBeVisible();

  await show(page, "DEMO-11");
  await expect(card(page)).toContainText("Triage says it is the code, 81%.");
  await expect(card(page).getByRole("list", { name: "Latest progress" })).toBeVisible();
  await shot(page, "01-focus-failed-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-focus-failed-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await show(page, "DEMO-9");
  await expect(card(page).getByRole("img", { name: "ready to merge" })).toBeVisible();
  await expect(card(page).locator("[data-pr-line]")).toContainText("PR #42 · checks green");
  await shot(page, "03-focus-ready.png");

  const total = Number((await card(page).locator("[data-pager] > span").first().textContent())?.split(" of ")[1]);
  for (let at = 0; at < total; at += 1) {
    const prev = card(page).getByRole("button", { name: "Previous item" });
    if (await prev.isDisabled()) break;
    await prev.click();
  }
  await card(page).getByRole("button", { name: "Next item" }).click();
  await expect(card(page)).toContainText(`2 of ${total}`);
  await expect(page.getByRole("list", { name: "Then" })).toBeVisible();
  await shot(page, "04-focus-rows.png", true);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(card(page)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await shot(page, "05-focus-390.png", true);
});
