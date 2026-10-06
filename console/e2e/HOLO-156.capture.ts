import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const fact = (name: string, ok: boolean, detail: string) => ({ name, ok, detail });
const readiness = (run: number, ticket: string, pr: number, approved: boolean) => ({
  run, ticket, pr_url: `https://github.com/example/demo/pull/${pr}`,
  head_sha: "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
  ready: approved, reason: approved ? null : "review_not_approved",
  detail: approved ? null : "GitHub's review decision is REVIEW_REQUIRED",
  facts: [
    fact("parked", true, `run ${run} is ${ticket}'s newest run, parked awaiting_merge_approval`),
    fact("human_approval", true, "[merge] approve is \"human\""),
    fact("review_approved", approved, `GitHub's review decision is ${approved ? "APPROVED" : "REVIEW_REQUIRED"}`),
    fact("checks_passed", true, "the required checks are success"),
    fact("mergeable", true, "GitHub's mergeable is MERGEABLE"),
    fact("threads_resolved", true, "no review thread is open"),
    fact("head_unchanged", true, "origin's branch and the pull request are at 5acc138"),
  ],
});

test("merge", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 },
    recordVideo: { dir: out, size: { width: 1440, height: 900 } } });
  const page = await context.newPage();
  await page.route("**/projects/demo/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const now = typeof body.now === "number" ? body.now : Date.now();
    const parked = (run: number, ticket: string, pr: number, title: string, review: string, ageMs: number) => ({
      kind: "pr_open", level: "attention", run, ticket, title, ticket_url: null,
      pr_url: `https://github.com/example/demo/pull/${pr}`,
      reason: "Waiting for a maintainer's merge", asked_ms: now - ageMs,
      pr: { number: pr, checks: "success", review, threads: 0, title },
    });
    body.items = [...body.items,
      parked(47, "DEMO-7", 31, "Show the merge ledger as a timeline", "approved", 40 * 60_000),
      parked(48, "DEMO-8", 32, "Name each worker's route on the Floor", "review_required", 12 * 60_000)];
    if (body.level === "none") body.level = "attention";
    await route.fulfill({ response, json: body });
  });
  await page.route("**/runs/47/merge", (route) => route.fulfill({ json: readiness(47, "DEMO-7", 31, true) }));
  await page.route("**/runs/48/merge", (route) => route.fulfill({ json: readiness(48, "DEMO-8", 32, false) }));
  await page.route("**/actions/merge", (route) => route.fulfill({
    json: { action: "merge", ok: true, run: 47, detail: "DEMO-7 approved: run 47 released" },
  }));

  await page.goto("/");
  const table = page.getByRole("region", { name: "Pull requests" });
  const ready = table.getByRole("row").filter({ hasText: "DEMO-7" });
  const waiting = table.getByRole("row").filter({ hasText: "DEMO-8" });
  await expect(ready.getByRole("button", { name: "Merge", exact: true })).toBeVisible();
  await expect(waiting.getByText("waiting on review approval")).toBeVisible();
  await table.scrollIntoViewIfNeeded();
  await table.screenshot({ path: path.join(out, "01-merge-button.png") });

  await ready.getByRole("button", { name: "Merge", exact: true }).click();
  await expect(ready.getByText("Merge PR #31 into main?")).toBeVisible();
  await expect(ready.getByRole("button", { name: "Confirm merge" })).toBeVisible();
  await expect(ready.getByRole("button", { name: "Cancel" })).toBeVisible();
  await table.screenshot({ path: path.join(out, "02-confirm-merge.png") });

  await ready.getByRole("button", { name: "Confirm merge" }).click();
  await expect(ready.getByRole("status")).toHaveText("DEMO-7 approved: run 47 released");
  await expect(ready.getByRole("button", { name: "Merge", exact: true })).toHaveCount(0);
  await table.screenshot({ path: path.join(out, "03-merge-accepted.png") });

  await expect(waiting.getByRole("button", { name: "Merge", exact: true })).toHaveCount(0);
  await table.screenshot({ path: path.join(out, "04-waiting-on-review.png") });
  await page.waitForTimeout(800);

  const video = page.video();
  await context.close();
  if (video) await video.saveAs(path.join(out, "merge-flow.webm"));
  if (video) await video.delete();
});
