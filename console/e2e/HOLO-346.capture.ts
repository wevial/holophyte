import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string, fullPage = false) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage });
}

const isFindings = (url: URL) => url.pathname.endsWith("/findings");

async function ready(page: Page) {
  await expect(page.locator("[data-count-line]")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("region", { name: "Clean reviews" })).toBeVisible();
}

test("01 the Findings page on the seeded daemon, Dawn and Night", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/findings");
  await ready(page);
  await shot(page, "01-findings-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-findings-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
});

test("02 the page scoped to demo, with its scope line and link", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/findings?project=demo");
  await ready(page);
  await expect(page.locator("[data-scope-line]")).toContainText("Showing demo only");
  await expect(page.getByRole("link", { name: "Show all projects" })).toBeVisible();
  await shot(page, "03-findings-scoped.png");
});

test("03 nothing open: the empty sentences and the No open findings verdict", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.route(isFindings, async (route) => {
    const since = Number(new URL(route.request().url()).searchParams.get("since"));
    const at = since + 3_600_000;
    const clean = (run: number, ticket: string) => ({
      run, ticket, title: `${ticket} title`, phase: "done", outcome: "merged", ended_ms: at, pr_url: null,
      rounds: [{ round: 1, started_ms: at, ended_ms: at, verdict: "pass", reviewer_model: "reviewer", findings: [], instructions: [], operator_notes: [] }],
      ledger: [], advisories: [],
    });
    await route.fulfill({ json: { since, limit: 100, runs: [clean(2, "DEMO-2"), clean(3, "DEMO-3")], follow_ups: [] } });
  });
  await page.goto("/next.html#/findings");
  await ready(page);
  await expect(page.locator("[data-verdict]")).toContainText("No open findings.");
  await expect(page.getByText("Nothing open.", { exact: true })).toBeVisible();
  await shot(page, "04-findings-empty.png");
});

test("04 the page at 390 px, nothing scrolling sideways", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/findings");
  await ready(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await shot(page, "05-findings-390.png", true);
});
