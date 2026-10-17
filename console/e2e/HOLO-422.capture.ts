import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { runTurnsSchema, transcriptSchema } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("turn transcript failures, tool expansion, missing and empty states", async ({ browser, baseURL }) => {
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
  let timedOut = false;
  await page.route(new RegExp(`/runs/${run}/turns$`), async route => {
    await route.fulfill({ json: runTurnsSchema.parse({ turns: [
      { ...common, id: 40, role: "implement", label: "claude opus", seconds: 372, cost_usd: 2.18 },
      { ...common, id: 41, role: "review", label: "codex gpt-6-sol", seconds: 100 },
      { ...common, id: 42, role: "implement", label: "claude opus", seconds: 185,
        input_tokens: 52100, output_tokens: 9300, cost_usd: 0.92, timed_out: timedOut },
    ] }) });
  });
  let state: "entries" | "missing" | "empty" = "entries";
  const entry = { tool: null, call: null, summary: null, failed: false, cut: false };
  const transcript = transcriptSchema.parse({ entries: [
    { ...entry, speaker: "user", text: "Fix the sweep so it frees a silent lease.\nPreserve the ownership check before releasing it.", at_ms: Date.parse("2026-10-16T23:45:12Z") },
    { ...entry, speaker: "assistant", text: "The sweep leaves the lease held when the writer stops responding. I’ll check the ownership guard and reproduce it with the focused tests.", at_ms: Date.parse("2026-10-16T23:45:18Z") },
    { ...entry, speaker: "error", text: "529 overloaded", at_ms: Date.parse("2026-10-16T23:46:24Z") },
    { ...entry, speaker: "command", tool: "Bash", call: "bash-1", summary: "python3 -m unittest discover -s tests -p 'test_sweep.py'", text: "python3 -m unittest discover -s tests -p 'test_sweep.py'", at_ms: Date.parse("2026-10-16T23:46:32Z") },
    { ...entry, speaker: "tool", tool: "Bash", call: "bash-1", failed: true, text: "FAIL: test_silent_writer_releases_lease\nAssertionError: lease remains held\n\nRan 4 tests in 2.3s\nFAILED (failures=1)", at_ms: Date.parse("2026-10-16T23:46:34.300Z") },
    { ...entry, speaker: "command", tool: "Edit", call: "edit-1", summary: "Guard the lease release", text: "Replace stale lease release with guarded release.", at_ms: Date.parse("2026-10-16T23:47:00Z") },
    { ...entry, speaker: "tool", tool: "Edit", call: "edit-1", text: "Updated sweep.py", at_ms: Date.parse("2026-10-16T23:47:01Z") },
  ] });
  await page.route(new RegExp(`/runs/${run}/turns/42/transcript$`), async route => {
    if (state === "missing") await route.fulfill({ status: 404, body: "not found" });
    else await route.fulfill({ json: state === "empty" ? transcriptSchema.parse({ entries: [] }) : transcript });
  });
  await page.goto(`/next.html#/runs/demo/${run}/turns/42`);
  await expect(page.getByRole("list", { name: "Turn transcript" })).toBeVisible();
  const edit = page.getByRole("listitem").filter({ has: page.getByText("Edit", { exact: true }) });
  await expect(edit.locator("details")).not.toHaveAttribute("open");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "01-turn-entries-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await shot(page, "02-turn-entries-dawn.png");
  await edit.locator("summary").click();
  await expect(edit.getByText("Updated sweep.py")).toBeVisible();
  await shot(page, "03-turn-entry-open.png");
  state = "missing";
  await page.reload();
  await expect(page.getByText("writer-host has no transcript for this turn.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the run" })).toBeVisible();
  await shot(page, "04-turn-no-transcript.png");
  state = "empty";
  timedOut = true;
  await page.reload();
  await expect(page.getByText("This turn has no entries. It timed out before the agent wrote anything.")).toBeVisible();
  await shot(page, "05-turn-no-entries.png");
  state = "entries";
  timedOut = false;
  await page.setViewportSize({ width: 390, height: 900 });
  await page.reload();
  await expect(page.getByRole("list", { name: "Turn transcript" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "06-turn-entries-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "turn-entries-flow.webm"));
    await video.delete();
  }
});
