import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { runTurnsSchema } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("turn header in night and dawn, unrecorded review usage and narrow viewport", async ({ browser, baseURL }) => {
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
  await page.goto(`/next.html#/runs/demo/${run}/turns/42`);
  await expect(page.getByRole("heading", { level: 1, name: "Implementer · turn 2" })).toBeVisible();
  await expect(page.getByText("run so far $3.10")).toBeVisible();
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "01-turn-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await shot(page, "02-turn-dawn.png");
  await page.getByRole("link", { name: "Previous turn" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Reviewer · round 1" })).toBeVisible();
  await expect(page.getByText("not recorded", { exact: true })).toHaveCount(2);
  await shot(page, "03-turn-unrecorded.png");
  await page.getByRole("link", { name: "Next turn" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Implementer · turn 2" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 900 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "04-turn-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "turn-flow.webm"));
    await video.delete();
  }
});
