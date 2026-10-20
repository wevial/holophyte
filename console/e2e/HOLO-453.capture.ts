import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

/** The seeded #42 as a draft held for review: every pull request the
 *  fixture parks was last seen ready. */
function asDraft(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(asDraft);
  if (value == null || typeof value !== "object") return value;
  const record = Object.fromEntries(Object.entries(value).map(([key, child]) => [key, asDraft(child)]));
  if (typeof record.pr_url === "string" && record.pr_url.endsWith("/pull/42") && record.pr != null && typeof record.pr === "object") {
    record.pr = { ...record.pr, state: "draft" };
  }
  return record;
}

test("a draft row's Mark ready for review and its confirm", async ({ page }) => {
  await page.route(/\/attention(\?.*)?$/, async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: asDraft(await response.json()) });
  });
  await page.goto("/next.html#/");
  const lane = page.getByRole("region", { name: "Pull requests", exact: true });
  const demo9 = lane.locator("[data-pr-row]").filter({ hasText: "DEMO-9" });
  await demo9.getByRole("button", { name: /^PR #42, / }).click();
  const markReady = demo9.getByRole("button", { name: "Mark ready for review" });
  await expect(markReady).toBeVisible();
  await expect(demo9.getByRole("link", { name: "Open PR to review" })).toBeVisible();
  await markReady.scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, 200));
  await shot(page, "01-mark-ready-button.png");

  await markReady.click();
  const dialog = page.getByRole("dialog", { name: "Mark #42 ready for review?" });
  await expect(dialog.getByText("Review bots and the full checks start on GitHub.")).toBeVisible();
  await shot(page, "02-mark-ready-confirm.png");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
});
