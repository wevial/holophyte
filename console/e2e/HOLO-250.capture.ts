import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const prUrl = "https://github.com/example/demo/pull/33";

test("queued row", async ({ page }) => {
  await page.route("**/projects/demo/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.runs = [];
    body.queued = [{
      ticket: "DEMO-9", ticket_url: null, title: "Show released work on the Floor",
      reason: "approved for merge; the next claim resumes the candidate at the merge gate",
      pr_url: prUrl, waiting_ms: 4 * 60_000,
    }];
    await route.fulfill({ response, json: body });
  });

  await page.goto("/");
  const floor = page.getByRole("region", { name: "Floor", exact: true });
  const row = floor.getByRole("listitem").filter({ hasText: "DEMO-9" });
  await expect(row.getByText("Queued", { exact: true })).toBeVisible();
  await expect(row.getByRole("link", { name: "PR #33" })).toHaveAttribute("href", prUrl);
  await expect(floor).toContainText("0 runs · 1 queued");
  await floor.scrollIntoViewIfNeeded();
  await floor.screenshot({ path: path.join(out, "01-queued-row.png") });
});
