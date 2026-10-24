import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import type { ScoutDetail, ScoutTask } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const second = 1000;
const minute = 60 * second;
const ASK = "Why do loop runs crash with ‘database is locked’?";

const task = (question: string, phase: ScoutTask["phase"], fields: Partial<ScoutTask> = {}): ScoutTask => ({
  id: 1, pass: 1, wave: 1, question, paths: [], mode: "read", route: "luna", phase,
  started_ms: null, ended_ms: null, duration_ms: null, tokens: null, cost_usd: null, excerpt: null, ...fields,
});

/** A bug scout started 14 minutes ago, swarming in wave 1 of 2. */
function running(now: number): ScoutDetail {
  const start = now - 14 * minute;
  const first = start + 72 * second;
  const done = (question: string, took: number, tokens: number) =>
    task(question, "done", { started_ms: first, ended_ms: first + took, duration_ms: took, tokens });
  const busy = (question: string, since: number, mode: ScoutTask["mode"] = "read") =>
    task(question, "running", { started_ms: now - since, mode });
  return {
    scout: {
      id: 31, ask: ASK, kind: "bug", phase: "swarming", reason: null, lead: "opus", helper: "luna", swarm: 4,
      tasks_done: 5, tasks_total: 10, created_ms: start - 20 * second, started_ms: start, ended_ms: null, elapsed_ms: 14 * minute,
      heartbeat_age_ms: 4000, base_sha: "a41c9e2b7d0f4c3e9a1b2c3d4e5f60718293a4b5", host: "writer",
      budget: { minutes: 30, helper_turns: 4, waves: 2 }, totals: null,
    },
    tasks: [
      done("Where does the loop open SQLite connections, and with what busy_timeout?", 108 * second, 38_000),
      done("Which writers hold a transaction open across a network call?", 123 * second, 41_000),
      done("Does the comment listener write to the store from its own thread?", 91 * second, 29_000),
      done("Is WAL mode set on every connection, or only the first?", 72 * second, 22_000),
      done("What do the last 7 days of ‘database is locked’ lines have in common?", 146 * second, 47_000),
      busy("Repro: two loop workers claim at once while a publish runs", 280 * second, "run"),
      busy("Repro: listener writes during a publish transaction", 185 * second, "run"),
      busy("Does busy_timeout survive the reconnect in lotuspod/store.py?", 70 * second),
      busy("Which tests already cover concurrent writers?", 22 * second),
      task("Does the scheduled digest open its own connection?", "queued"),
    ],
    reports: [],
    actions: [],
    events: [{ at: first - 2 * second, kind: "agent_turn", summary: "scout-lead turn ended" }],
  };
}

/** The same scout done after two waves, its second with one helper stopped at the budget. */
function finished(now: number): ScoutDetail {
  const live = running(now);
  const start = now - 40 * minute;
  const at = (minutes: number, seconds = 0) => start + minutes * minute + seconds * second;
  const finding = { title: "The listener holds the write lock across publish", status: "shown" as const, evidence: [] };
  const action = { title: "Commit the listener's write before it publishes", why: "F1", kind: "fix" as const, body: "" };
  const summary = "Reproduced in a failing test: the comment listener holds the write lock while it publishes, so loop claims time out.";
  return {
    ...live,
    scout: { ...live.scout, id: 32, phase: "done", reason: "the lead wrote its report", tasks_done: 11, tasks_total: 12, started_ms: start, ended_ms: at(33, 42), elapsed_ms: 33 * minute },
    tasks: [
      ...live.tasks.map((each, index) => ({
        ...each, phase: "done" as const, started_ms: at(1, 12), ended_ms: index === 9 ? at(17, 32) : at(3 + index), duration_ms: (100 + index * 9) * second, tokens: (22 + index * 3) * 1000,
      })),
      task("Does publish need the transaction open, or can it run after commit?", "done", { wave: 2, started_ms: at(20, 12), ended_ms: at(22, 22), duration_ms: 130 * second, tokens: 34_000 }),
      task("Does the stall line up with disk write latency in the host log?", "stopped", { wave: 2, started_ms: at(22, 12), ended_ms: at(30, 12), duration_ms: 8 * minute }),
    ],
    reports: [{ pass: 1, at: at(33, 42), report: { summary, findings: [finding, finding, finding, finding], actions: [action, action, action, action], reproduction: null, cut: null } }],
    events: [
      { at: at(1, 10), kind: "agent_turn", summary: "scout-lead turn ended" },
      { at: at(20, 10), kind: "agent_turn", summary: "scout-lead turn ended" },
      { at: at(33, 40), kind: "agent_turn", summary: "scout-lead turn ended" },
    ],
  };
}

/** `/scouts/31` and `/scouts/32` answered with the seeded scouts, timed off the browser's clock. */
async function seed(page: Page) {
  const now = await page.evaluate(() => Date.now());
  await page.route(/\/scouts\/31$/, (route) => route.fulfill({ json: running(now) }));
  await page.route(/\/scouts\/32$/, (route) => route.fulfill({ json: finished(now) }));
}

async function open(page: Page, scout: number, width: number, scheme: "light" | "dark", height = 1100) {
  await page.setViewportSize({ width, height });
  await page.emulateMedia({ colorScheme: scheme });
  await page.goto("/next.html");
  await seed(page);
  await page.goto(`/next.html#/scouts/demo/${scout}`);
  await expect(page.getByRole("list", { name: "Scout activity" })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

test.use({ timezoneId: "America/Los_Angeles" });

const activity = (page: Page) => page.getByRole("list", { name: "Scout activity" });

test("01 a bug scout swarming in wave 1 of 2, Dawn", async ({ page }) => {
  await open(page, 31, 1440, "light");
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  await expect(activity(page).getByRole("list", { name: "Helpers" }).getByRole("listitem")).toHaveCount(10);
  await page.screenshot({ path: path.join(out, "01-scout-running-dawn.png"), fullPage: true });
});

test("02 a done scout with two waves, Dawn", async ({ page }) => {
  await open(page, 32, 1440, "light", 900);
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toHaveCount(0);
  await expect(activity(page).locator("[data-group-title]")).toHaveCount(5);
  await expect(activity(page).getByRole("list", { name: "Helpers" }).getByRole("listitem")).toHaveCount(2);
  await page.screenshot({ path: path.join(out, "02-scout-done-dawn.png"), fullPage: true });
});

test("03 the running scout's Stop confirm with its reason filled", async ({ page }) => {
  await open(page, 31, 1440, "light", 900);
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Stop scout 31?" });
  await dialog.getByRole("textbox").fill("Wrong project: this crash is lotuspod's sibling's.");
  await expect(dialog.getByRole("button", { name: "Stop scout" })).toBeEnabled();
  await page.screenshot({ path: path.join(out, "03-scout-stop-dialog.png") });
});

test("04 the running scout in Night", async ({ page }) => {
  await open(page, 31, 1440, "dark");
  await page.screenshot({ path: path.join(out, "04-scout-running-night.png"), fullPage: true });
});

test("05 the running scout at phone width, Dawn", async ({ page }) => {
  await open(page, 31, 390, "light");
  await expect(activity(page).getByRole("list", { name: "Helpers" })).toBeVisible();
  await page.screenshot({ path: path.join(out, "05-scout-phone.png"), fullPage: true });
});
