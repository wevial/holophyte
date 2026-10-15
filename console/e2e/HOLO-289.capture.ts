import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const fact = (name: string, ok: boolean, detail: string) => ({ name, ok, detail });
const readiness = (run: number) => ({
  run, ticket: "DEMO-9", pr_url: null, head_sha: "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
  ready: true, reason: null, detail: null,
  facts: [
    fact("parked", true, `run ${run} is DEMO-9's newest run, parked awaiting_merge_approval`),
    fact("human_approval", true, "[merge] approve is \"human\""),
    fact("review_approved", true, "GitHub's review decision is APPROVED"),
    fact("checks_passed", true, "the required checks are success"),
    fact("mergeable", true, "GitHub's mergeable is MERGEABLE"),
    fact("threads_resolved", true, "no review thread is open"),
    fact("head_unchanged", true, "origin's branch and the pull request are at 5acc138"),
  ],
});

/** DEMO-9's merge readiness answers ready, so its row wears the tag. */
async function readyDemo9(page: Page) {
  let demo9: number | null = null;
  await page.route("**/projects/demo/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const item = body.items.find((candidate: { ticket?: string }) => candidate.ticket === "DEMO-9");
    if (item) demo9 = item.run;
    await route.fulfill({ response, json: body });
  });
  await page.route("**/runs/*/merge", async (route) => {
    const run = Number(route.request().url().match(/\/runs\/(\d+)\/merge$/)?.[1]);
    if (run === demo9) await route.fulfill({ json: readiness(run) });
    else await route.continue();
  });
}

const prs = (page: Page) => page.getByRole("region", { name: "Pull requests", exact: true });

test("the new Overview's Pull requests section: lanes, rows, the needs-you tag and an opened row", async ({ page }) => {
  await readyDemo9(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/");
  const section = prs(page);
  const demo8 = section.getByRole("button", { name: /, DEMO-8 / });
  await expect(demo8).toBeVisible();
  await expect(section.getByRole("button", { name: /, DEMO-9 / })).toBeVisible();
  await expect(section.getByRole("button", { name: "Show DEMO-9 in Needs you" })).toBeVisible();
  await section.scrollIntoViewIfNeeded();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-prs-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await section.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, "02-prs-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await demo8.click();
  await expect(demo8).toHaveAttribute("aria-expanded", "true");
  await expect(section.getByRole("region", { name: "Pull request facts" })).toBeVisible();
  await expect(section.getByRole("button", { name: "Ask", exact: true })).toBeVisible();
  await section.screenshot({ path: path.join(out, "03-prs-open.png") });
  await demo8.click();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(demo8).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await section.screenshot({ path: path.join(out, "04-prs-390.png") });
});
