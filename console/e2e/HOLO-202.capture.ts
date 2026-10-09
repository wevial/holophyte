import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const prUrl = "https://github.com/example/demo/pull/32";
const question = "Why is the guest keyed by name?";
const fact = (name: string, detail: string) => ({ name, ok: true, detail });
const readiness = {
  run: 48, ticket: "DEMO-8", pr_url: prUrl, head_sha: "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
  ready: true, reason: null, detail: null,
  facts: [
    fact("parked", "run 48 is DEMO-8's newest run, parked awaiting_merge_approval"),
    fact("human_approval", "[merge] approve is \"human\""),
    fact("review_approved", "GitHub's review decision is APPROVED"),
    fact("checks_passed", "the required checks are success"),
    fact("mergeable", "GitHub's mergeable is MERGEABLE"),
    fact("threads_resolved", "no review thread is open"),
    fact("head_unchanged", "origin's branch and the pull request are at 5acc138"),
  ],
};

test("ask", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 },
    recordVideo: { dir: out, size: { width: 1440, height: 900 } } });
  const page = await context.newPage();
  let asks: Record<string, unknown>[] = [];
  await page.route("**/projects/demo/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const now = typeof body.now === "number" ? body.now : Date.now();
    body.items = [...body.items, {
      kind: "pr_open", level: "attention", run: 48, ticket: "DEMO-8",
      title: "Name each worker's route on the Floor", ticket_url: null, pr_url: prUrl,
      reason: "Waiting for a maintainer's merge", asked_ms: now - 12 * 60_000,
      pr: { number: 32, checks: "success", review: "approved", threads: 0,
        title: "Name each worker's route on the Floor" },
    }];
    if (body.level === "none") body.level = "attention";
    await route.fulfill({ response, json: body });
  });
  await page.route("**/runs/48/merge", (route) => route.fulfill({ json: readiness }));
  await page.route("**/runs/48/asks", (route) => route.fulfill({
    json: { run: 48, ticket: "DEMO-8", pr_url: prUrl, asks } }));
  await page.route("**/actions/ask", async (route) => {
    asks = [{ id: 812, question, author: "maintainer", asked_ms: Date.now(), answered_ms: null, url: null, answer: null }];
    await route.fulfill({ json: { action: "ask", ok: true, run: 48, ticket: "DEMO-8", event_id: 812, recorded: 1,
      detail: `console ask event 812 recorded; the loop's next claim answers it on ${prUrl}` } });
  });

  await page.goto("/");
  const table = page.getByRole("region", { name: "Pull requests" });
  const row = table.getByRole("row").filter({ hasText: "DEMO-8" });
  await row.getByRole("button", { name: "Ask", exact: true }).click();
  await row.getByRole("textbox", { name: "Question about the pull request" }).fill(question);
  await expect(row.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await expect(row.getByRole("button", { name: "Cancel" })).toBeVisible();
  await table.scrollIntoViewIfNeeded();
  await table.screenshot({ path: path.join(out, "01-ask-group-open.png") });

  await row.getByRole("button", { name: "Send", exact: true }).click();
  await expect(row.getByText(`Asked: ${question}`)).toBeVisible();
  await expect(row.getByText("waiting for the answer")).toBeVisible();
  await expect(row.getByRole("button", { name: "Ask", exact: true })).toHaveCount(0);
  await table.screenshot({ path: path.join(out, "02-ask-waiting.png") });
  await page.waitForTimeout(800);

  asks = [{ ...asks[0], answered_ms: Date.now(), url: `${prUrl}#issuecomment-901`,
    answer: "Names are unique per party, so the guest list keys on them: `src/app.py:30`." }];
  await page.reload();
  const answered = page.getByRole("region", { name: "Pull requests" });
  const answeredRow = answered.getByRole("row").filter({ hasText: "DEMO-8" });
  await expect(answeredRow.getByRole("link", { name: "Answer on PR #32" })).toBeVisible();
  await expect(answeredRow.getByText("Names are unique per party")).toBeVisible();
  await answered.scrollIntoViewIfNeeded();
  await answered.screenshot({ path: path.join(out, "03-ask-answered.png") });
  await page.waitForTimeout(800);

  const video = page.video();
  await context.close();
  if (video) await video.saveAs(path.join(out, "ask-flow.webm"));
  if (video) await video.delete();
});
