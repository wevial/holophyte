import path from "node:path";
import { expect, test } from "@playwright/test";
import type { Attention } from "../src/lib/contract.gen";

test("Overview pull requests in Night and Dawn", async ({ page }) => {
  await page.route("**/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json() as Attention;
    if (!Array.isArray(body.items)) return route.fulfill({ response });
    const items = body.items.map((item) => {
      if (item.kind !== "pr_open") return item;
      const state = item.pr?.number === 42 ? "draft" : "ready";
      return {
        ...item, park_kind: "pull_request",
        pr: { ...item.pr, state, checks: "success", review: "review_required", threads: 0, open_threads: 0 },
      };
    });
    await route.fulfill({ response, json: { ...body, items } });
  });
  await page.route("**/runs/*/merge", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), ready: false } });
  });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/next.html#/");
  const lane = page.getByRole("region", { name: "Pull requests", exact: true });
  await expect(lane.getByText("Draft", { exact: true })).toBeVisible();
  await expect(lane.getByText("Ready", { exact: true })).toBeVisible();
  await expect(lane.locator("[data-pr-state]")).toHaveText([
    "draft · held for your review", "parked · review pending",
  ]);
  await page.evaluate(() => document.fonts.ready);
  await lane.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(process.env.CAPTURE_OUT ?? "", "01-pull-requests-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await lane.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(process.env.CAPTURE_OUT ?? "", "02-pull-requests-dawn.png") });
});
