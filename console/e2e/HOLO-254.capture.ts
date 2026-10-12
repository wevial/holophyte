import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

function prItem(now: number, run: number, n: number, park_kind: string, checks: string, reason: string) {
  const pr_url = `https://github.com/example/demo/pull/${n}`;
  return {
    kind: "pr_open", level: "attention", run, ticket: `DEMO-${run}`, park_kind,
    title: `Demo change ${run}`, ticket_url: null, pr_url, reason, asked_ms: now - 12 * 60_000,
    pr: { number: n, checks, review: "approved", threads: 0, open_threads: 0, title: `Demo change ${run}` },
  };
}

test("waiting on CI", async ({ page }) => {
  await page.route("**/projects/demo/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const now = typeof body.now === "number" ? body.now : Date.now();
    body.items = [...body.items,
      prItem(now, 61, 41, "ci", "pending", "PR open\npending checks: unit"),
      prItem(now, 62, 42, "pull_request", "success", "Waiting for a maintainer's merge")];
    if (body.level === "none") body.level = "attention";
    await route.fulfill({ response, json: body });
  });

  await page.goto("/");
  const table = page.getByRole("region", { name: "Pull requests" });
  const ci = table.getByRole("row").filter({ hasText: "DEMO-61" });
  await expect(ci.getByText("Waiting on CI · checks running")).toBeVisible();
  const other = table.getByRole("row").filter({ hasText: "DEMO-62" });
  await expect(other.getByText("Waiting for a maintainer's merge")).toBeVisible();
  await table.scrollIntoViewIfNeeded();
  await table.screenshot({ path: path.join(out, "01-waiting-on-ci.png") });
});
