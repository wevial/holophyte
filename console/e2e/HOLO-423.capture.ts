import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { runTurnsSchema, transcriptSchema } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("turn search, errors, empty results and narrow toolbar", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 },
    colorScheme: "light", timezoneId: "America/Los_Angeles",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } } });
  const page = await context.newPage();
  const response = await page.request.get("/projects/demo/history?since=0");
  expect(response.ok()).toBe(true);
  const { rows } = await response.json() as { rows: { id: number; ticket: string }[] };
  const ids = rows.filter(row => row.ticket === "DEMO-7").map(row => row.id);
  expect(ids.length).toBeGreaterThan(0);
  const run = Math.max(...ids);
  await page.route(new RegExp(`/runs/${run}$`), async route => {
    const answer = await route.fetch();
    const body = await answer.json();
    await route.fulfill({ response: answer, json: { ...body, run: {
      ...body.run, title: "The sweep frees a silent lease", host: "writer-host",
    } } });
  });
  const common = { route: "primary", session_id: null, ended_ms: Date.parse("2026-10-16T23:48:17Z"),
    exit_status: 0, timed_out: false, input_tokens: null, output_tokens: null, cost_usd: null };
  await page.route(new RegExp(`/runs/${run}/turns$`), async route => {
    await route.fulfill({ json: runTurnsSchema.parse({ turns: [
      { ...common, id: 40, role: "implement", label: "claude opus", seconds: 372, cost_usd: 2.18 },
      { ...common, id: 41, role: "review", label: "codex gpt-6-sol", seconds: 100 },
      { ...common, id: 42, role: "implement", label: "claude opus", seconds: 185,
        input_tokens: 52100, output_tokens: 9300, cost_usd: 0.92 },
    ] }) });
  });
  const entry = { tool: null, call: null, summary: null, failed: false, cut: false };
  const start = Date.parse("2026-10-16T23:45:12Z");
  const transcript = transcriptSchema.parse({ entries: [
    { ...entry, speaker: "user", text: "Fix the sweep and preserve the ownership check.", at_ms: start + 0 },
    { ...entry, speaker: "assistant", text: "I will reproduce test_sweep before editing.", at_ms: start + 1000 },
    { ...entry, speaker: "command", tool: "Read", call: "read", summary: "Inspect lease ownership", text: "Read sweep.py", at_ms: start + 2000 },
    { ...entry, speaker: "tool", tool: "Read", call: "read", text: "Lease ownership guard", at_ms: start + 3000 },
    { ...entry, speaker: "command", tool: "Bash", call: "bash", summary: "Run focused tests", text: "python3 -m unittest tests.test_lease", at_ms: start + 4000 },
    { ...entry, speaker: "tool", tool: "Bash", call: "bash", text: "FAIL: lease remains held", failed: true, at_ms: start + 5000 },
    { ...entry, speaker: "error", text: "529 overloaded", at_ms: start + 6000 },
    { ...entry, speaker: "command", tool: "Edit", call: "edit", summary: "Guard the lease release", text: "Replace stale lease release with guarded release.", at_ms: start + 7000 },
    { ...entry, speaker: "tool", tool: "Edit", call: "edit", text: "test_sweep now passes. Verified test_sweep again.", at_ms: start + 8000 },
    { ...entry, speaker: "command", tool: "Bash", call: "verify", summary: "Verify the patch", text: "ruff check .", at_ms: start + 9000 },
    { ...entry, speaker: "tool", tool: "Bash", call: "verify", text: "All checks passed!", at_ms: start + 10000 },
  ] });
  await page.route(new RegExp(`/runs/${run}/turns/42/transcript$`), async route => {
    await route.fulfill({ json: transcript });
  });
  await page.goto(`/next.html#/runs/demo/${run}/turns/42`);
  await expect(page.getByRole("list", { name: "Turn transcript" })).toBeVisible();
  const edit = page.getByRole("listitem").filter({ has: page.getByText("Edit", { exact: true }) });
  await expect(edit.locator("details")).not.toHaveAttribute("open");
  const search = page.getByRole("searchbox", { name: "Search this turn" });
  await search.fill("Test_Sweep");
  await expect(page.getByText("3 matches in 2 entries")).toBeVisible();
  await expect(edit.locator("details")).toHaveAttribute("open", "");
  await expect(page.locator("mark")).toHaveCount(3);
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "01-turn-search-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await shot(page, "02-turn-search-dawn.png");
  await search.fill("");
  await page.getByRole("button", { name: "Errors 2", exact: true }).click();
  await expect(page.getByRole("list", { name: "Turn transcript" }).getByRole("listitem")).toHaveCount(2);
  await shot(page, "03-turn-errors.png");
  await page.getByRole("button", { name: "All 8", exact: true }).click();
  await search.fill("zzz");
  await expect(page.getByText("Nothing in this turn matches “zzz”.")).toBeVisible();
  await shot(page, "04-turn-no-match.png");
  await search.fill("Test_Sweep");
  await page.setViewportSize({ width: 390, height: 900 });
  await expect(page.getByText("3 matches in 2 entries")).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "05-turn-search-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "turn-search-flow.webm"));
    await video.delete();
  }
});
