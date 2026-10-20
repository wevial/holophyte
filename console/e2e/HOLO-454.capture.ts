import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { runAsksSchema } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

const answer = [
  "**When the prompt shows.** In `components/x.tsx:104`, the modal opens only after the lease is held, so a second tab never sees it.",
  "**Why it waited.** The sweep in `holophyte/sweep.py:212` releases a silent lease after its grace period; until then the modal stays closed.",
  "What changed in this run:",
  "- the guard now checks ownership before release",
  "- the dry run prints one line per lease",
  "- `test_sweep.py` covers the silent writer",
].join("\n\n");

test("the Run page's agent chat renders a markdown answer with room to read", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 },
    colorScheme: "light", timezoneId: "America/Los_Angeles" });
  const page = await context.newPage();
  const response = await page.request.get("/projects/demo/history?since=0");
  expect(response.ok()).toBe(true);
  const { rows } = await response.json() as { rows: { id: number; ticket: string }[] };
  expect(rows.length).toBeGreaterThan(0);
  const run = rows[0]!;
  await page.route(new RegExp(`/runs/${run.id}/asks$`), async route => {
    await route.fulfill({ json: runAsksSchema.parse({ run: run.id, ticket: run.ticket, pr_url: null, asks: [{
      id: 1, author: "maintainer", question: "When does the prompt show, and why did it wait?",
      asked_ms: Date.parse("2026-10-16T23:40:00Z"), answered_ms: Date.parse("2026-10-16T23:43:00Z"),
      answer, url: "https://github.com/example/project/pull/12#issuecomment-42",
    }] }) });
  });
  await page.goto(`/next.html#/runs/demo/${run.id}`);
  const panel = page.getByRole("complementary", { name: "Agent" });
  await expect(panel.locator("strong", { hasText: "When the prompt shows." })).toBeVisible();
  await expect(panel.getByRole("link", { name: "Open on GitHub" })).toBeVisible();
  const box = await panel.boundingBox();
  expect(box!.width).toBeGreaterThanOrEqual(380);
  await shot(page, "01-agent-chat-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect((await panel.boundingBox())!.width).toBeGreaterThanOrEqual(380);
  await shot(page, "02-agent-chat-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await page.setViewportSize({ width: 390, height: 900 });
  await page.reload();
  await expect(panel.locator("strong", { hasText: "When the prompt shows." })).toBeVisible();
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, "03-agent-chat-390.png"), fullPage: true });
  await context.close();
});
