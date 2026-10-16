import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const at = (time: string) => Date.parse(`2026-10-14T${time}:00Z`);
const agent = (page: Page) => page.getByRole("complementary", { name: "Agent", exact: true });

test.use({ timezoneId: "UTC" });

async function shot(page: Page, name: string, fullPage = false) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage });
}

async function runOf(page: Page, ticket: string): Promise<number> {
  const response = await page.request.get("/projects/demo/history?since=0");
  expect(response.ok()).toBe(true);
  const { rows } = await response.json() as { rows: { id: number; ticket: string }[] };
  const ids = rows.filter((row) => row.ticket === ticket).map((row) => row.id);
  expect(ids.length).toBeGreaterThan(0);
  return Math.max(...ids);
}

async function thread(page: Page, run: number) {
  await page.route(new RegExp(`/runs/${run}$`), async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const note = { author: "maintainer", run, interrupt: false, withdrawn: false };
    await route.fulfill({ response, json: {
      ...body,
      run: { ...body.run, phase: "reviewing", outcome: null, ended_ms: null },
      steer_notes: [
        { ...note, id: 9001, kind: "amendment", note: "Keep the dry run read-only; reuse --act.", at: at("16:40"), consumed_by: run, consumed_ms: at("16:41") },
        { ...note, id: 9002, kind: "hint", note: "The existing fixture covers the empty board too.", at: at("16:50"), consumed_by: null, consumed_ms: null },
      ],
    } });
  });
  await page.route(new RegExp(`/runs/${run}/asks$`), (route) => route.fulfill({ json: {
    run, ticket: "DEMO-7", pr_url: null,
    asks: [{ id: 9003, author: "maintainer", question: "Does the dry run leave the board untouched?", asked_ms: at("16:53"), answered_ms: at("16:55"), answer: "Yes. The dry run only reports the proposed changes; --act applies them.", url: "https://github.com/example/demo/issues/7#issuecomment-9003" }],
  } }));
  await page.route(/\/projects\/demo\/status$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: {
      ...body,
      route_labels: { ...body.route_labels, implementer: "claude opus" },
      runs: body.runs.map((row: { id: number }) => row.id === run ? { ...row, phase: "reviewing", round: 1 } : row),
    } });
  });
}

async function open(page: Page, run: number, width = 1440) {
  await page.setViewportSize({ width, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`/next.html#/runs/demo/${run}`);
  await expect(agent(page)).toBeVisible();
}

test("01 reviewing agent thread in night and dawn", async ({ page }) => {
  const run = await runOf(page, "DEMO-7");
  await thread(page, run);
  await open(page, run);
  await expect(agent(page)).toContainText("implementer · claude opus");
  await expect(agent(page)).toContainText("idle until review round 2 returns");
  await expect(agent(page)).toContainText("You · steer · 16:40");
  await expect(agent(page)).toContainText("→ the agent read it at 16:41");
  await expect(agent(page)).toContainText("You · hint · 16:50");
  await expect(agent(page)).toContainText("→ waiting until the agent reads it");
  await expect(agent(page)).toContainText("Agent · 16:55");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-agent-thread-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await shot(page, "02-agent-thread-dawn.png");
});

test("02 ended agent with nothing sent", async ({ page }) => {
  const run = await runOf(page, "DEMO-11");
  await page.route(new RegExp(`/runs/${run}$`), async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, steer_notes: [] } });
  });
  await page.route(new RegExp(`/runs/${run}/asks$`), (route) => route.fulfill({ json: { run, ticket: "DEMO-11", pr_url: null, asks: [] } }));
  await open(page, run);
  await expect(agent(page)).toContainText("stopped with the run");
  await expect(agent(page)).toContainText("Nothing sent to this run yet.");
  await shot(page, "03-agent-empty.png");
});

test("03 agent stacks below the main column at 390 px", async ({ page }) => {
  const run = await runOf(page, "DEMO-7");
  await thread(page, run);
  await open(page, run, 390);
  await expect(agent(page)).toContainText("Agent · 16:55");
  const progress = await page.getByRole("region", { name: "Progress", exact: true }).boundingBox();
  const panel = await agent(page).boundingBox();
  expect(progress).not.toBeNull();
  expect(panel).not.toBeNull();
  expect(panel!.y).toBeGreaterThanOrEqual(progress!.y + progress!.height);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
  await shot(page, "04-agent-390.png", true);
});
