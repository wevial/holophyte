import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const prUrl = "https://github.com/example/demo/pull/32";

test("open threads", async ({ page }) => {
  await page.route("**/projects/demo/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const now = typeof body.now === "number" ? body.now : Date.now();
    body.items = [...body.items, {
      kind: "pr_open", level: "attention", run: 48, ticket: "DEMO-8",
      title: "Name each worker's route on the Floor", ticket_url: null, pr_url: prUrl,
      reason: "Waiting for a maintainer's merge", asked_ms: now - 12 * 60_000,
      pr: { number: 32, checks: "success", review: "approved", threads: 4, open_threads: 0,
        title: "Name each worker's route on the Floor" },
    }];
    if (body.level === "none") body.level = "attention";
    await route.fulfill({ response, json: body });
  });
  await page.route("**/runs/48", (route) => route.fulfill({ json: {
    run: { id: 48, ticket: "DEMO-8", phase: "awaiting_merge_approval", started_ms: Date.now() - 3_600_000,
      ended_ms: null, time_box_ms: null, host: null },
    rounds: [], events: [],
  } }));

  await page.goto("/");
  const table = page.getByRole("region", { name: "Pull requests" });
  const row = table.getByRole("row").filter({ hasText: "DEMO-8" });
  await expect(row.getByText("no open threads")).toBeVisible();
  await table.getByRole("button", { name: "Details for DEMO-8" }).click();
  const facts = table.getByRole("region", { name: "Pull request facts" });
  await expect(facts).toContainText("Open threads: 0");
  await table.scrollIntoViewIfNeeded();
  await table.screenshot({ path: path.join(out, "01-open-threads.png") });
});
