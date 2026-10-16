import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { runFilesSchema, runTurnsSchema } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

test("seeded reviewing run with detail route fixtures: closed, open night and dawn, no diff", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 },
    colorScheme: "light", timezoneId: "UTC", recordVideo: { dir: out, size: { width: 1440, height: 1000 } } });
  const page = await context.newPage();
  const response = await page.request.get("/projects/demo/history?since=0");
  expect(response.ok()).toBe(true);
  const { rows } = await response.json() as { rows: { id: number; ticket: string }[] };
  const ids = rows.filter(row => row.ticket === "DEMO-7").map(row => row.id);
  expect(ids.length).toBeGreaterThan(0);
  const run = Math.max(...ids);
  const at = (time: string) => Date.parse(`2026-10-14T${time}:00Z`);
  await page.route(new RegExp(`/runs/${run}$`), async route => {
    const answer = await route.fetch();
    const body = await answer.json();
    await route.fulfill({ response: answer, json: { ...body, run: { ...body.run, max_rounds: 3 }, rounds: [
      { round: 1, started_ms: at("16:40"), ended_ms: at("16:45"), verdict: "changes_requested",
        reviewer_model: "codex gpt-6-sol", instructions: [], operator_notes: [],
        findings: [{ path: "holophyte/serve/server.py", line: 12, severity: "p1", message: "The route is unmatched." }] },
      { round: 2, started_ms: at("16:50"), ended_ms: null, verdict: "", reviewer_model: null,
        findings: [], instructions: [], operator_notes: [] },
    ] } });
  });
  let noDiff = false;
  await page.route(new RegExp(`/runs/${run}/files$`), async route => {
    if (noDiff) {
      await route.fulfill({ status: 409, json: { error: "No branch or merge to diff." } });
      return;
    }
    await route.fulfill({ json: runFilesSchema.parse({ run, base: "main", head: "task/demo-7", truncated: false,
      total_added: 123, total_deleted: 16, files: [
        { path: "holophyte/serve/server.py", status: "M", added: 61, deleted: 9 },
        { path: "holophyte/config/agents.py", status: "M", added: 24, deleted: 7 },
        { path: "tests/test_agent_routes.py", status: "A", added: 32, deleted: 0 },
        { path: "docs/config.md", status: "M", added: 6, deleted: 0 },
      ] }) });
  });
  await page.route(new RegExp(`/runs/${run}/turns$`), async route => {
    await route.fulfill({ json: runTurnsSchema.parse({ turns: [
      { id: 1, role: "implement", label: "claude opus", route: "primary", seconds: 372, session_id: null },
      { id: 2, role: "review", label: "codex gpt-6-sol", route: "primary", seconds: 89, session_id: null },
      { id: 3, role: "implement", label: "claude opus", route: "primary", seconds: null, session_id: null },
    ] }) });
  });
  await page.goto(`/next.html#/runs/demo/${run}`);
  const fold = page.locator("details").filter({ has: page.locator("summary", { hasText: "Details" }) });
  const summary = fold.locator("summary");
  await expect(summary).toBeVisible();
  await expect(fold).not.toHaveAttribute("open");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await summary.scrollIntoViewIfNeeded();
  await shot(page, "01-details-closed.png");
  await summary.click();
  await expect(fold).toContainText("Files changed · 4");
  await expect(fold).toContainText("Transcript · 3 turns");
  await expect(fold).toContainText("6m 12s");
  await fold.evaluate(element => element.scrollIntoView({ block: "center" }));
  await shot(page, "02-details-open-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await fold.evaluate(element => element.scrollIntoView({ block: "center" }));
  await shot(page, "03-details-open-dawn.png");
  noDiff = true;
  await page.reload();
  await summary.click();
  await expect(fold).toContainText("No branch or merge to diff.");
  await fold.evaluate(element => element.scrollIntoView({ block: "center" }));
  await shot(page, "04-details-no-diff.png");
  const video = page.video();
  await context.close();
  if (video) await video.saveAs(path.join(out, "details-flow.webm"));
  if (video) await video.delete();
});
