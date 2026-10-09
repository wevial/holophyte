import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const fact = (name: string, ok: boolean, detail: string) => ({ name, ok, detail });
const review = "GitHub's review decision is REVIEW_REQUIRED; the host's GitHub user may bypass it:"
  + " ruleset human-review asks 1 approving review, current_user_can_bypass pull_requests_only";
const bypassable = {
  run: 48, ticket: "DEMO-8", pr_url: "https://github.com/example/demo/pull/32",
  head_sha: "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
  ready: false, reason: "review_bypassable", detail: review,
  facts: [
    fact("parked", true, "run 48 is DEMO-8's newest run, parked awaiting_merge_approval"),
    fact("human_approval", true, "[merge] approve is \"human\""),
    fact("review_approved", false, review),
    fact("checks_passed", true, "the required checks are success"),
    fact("mergeable", true, "GitHub's mergeable is MERGEABLE"),
    fact("threads_resolved", true, "no review thread is open"),
    fact("head_unchanged", true, "origin's branch and the pull request are at 5acc138"),
  ],
};

test("bypass merge", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 },
    recordVideo: { dir: out, size: { width: 1440, height: 900 } } });
  const page = await context.newPage();
  await page.route("**/projects/demo/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const now = typeof body.now === "number" ? body.now : Date.now();
    body.items = [...body.items, {
      kind: "pr_open", level: "attention", run: 48, ticket: "DEMO-8",
      title: "Name each worker's route on the Floor", ticket_url: null,
      pr_url: "https://github.com/example/demo/pull/32",
      reason: "Waiting for a maintainer's merge", asked_ms: now - 12 * 60_000,
      pr: { number: 32, checks: "success", review: "review_required", threads: 0,
        title: "Name each worker's route on the Floor" },
    }];
    if (body.level === "none") body.level = "attention";
    await route.fulfill({ response, json: body });
  });
  await page.route("**/runs/48/merge", (route) => route.fulfill({ json: bypassable }));

  await page.goto("/");
  const table = page.getByRole("region", { name: "Pull requests" });
  const row = table.getByRole("row").filter({ hasText: "DEMO-8" });
  const bypass = row.getByRole("button", { name: "Merge (bypass review)" });
  await expect(bypass).toBeVisible();
  await expect(row.getByRole("button", { name: "Merge", exact: true })).toHaveCount(0);
  await table.scrollIntoViewIfNeeded();
  await table.screenshot({ path: path.join(out, "01-bypass-merge-button.png") });

  await bypass.click();
  await expect(row.getByText("Bypass the required review and merge PR #32 into main?")).toBeVisible();
  await expect(row.getByText(review)).toBeVisible();
  await expect(row.getByRole("button", { name: "Confirm bypass merge" })).toBeVisible();
  await expect(row.getByRole("button", { name: "Cancel" })).toBeVisible();
  await table.screenshot({ path: path.join(out, "02-bypass-confirm.png") });
  await page.waitForTimeout(800);

  const video = page.video();
  await context.close();
  if (video) await video.saveAs(path.join(out, "bypass-merge-flow.webm"));
  if (video) await video.delete();
});
