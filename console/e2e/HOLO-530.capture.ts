import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import type { ScoutAction, ScoutDetail, ScoutTask } from "../src/lib/contract.gen";

const out = process.env.CAPTURE_OUT ?? "";
const second = 1000;
const minute = 60 * second;
const ASK = "Why do loop runs crash with ‘database is locked’?";
const REFUSAL = "'Depends on' names no ticket of this project: DEMO-9";

const draft = (title: string, summary: string, depends = "") =>
  `+++\nui_change = "none"\nestimate = 30\ndepends_on = [${depends}]\n+++\n# ${title}\n\n## Summary\n\n${summary}\n\n` +
  `## What / Why / How\n\n**What:** ${summary}\n\n**Why:** Loop claims time out while it waits.\n\n**How:** Through the store's writer.\n\n` +
  `## In scope\n\n- demo/store.py\n\n## Out of scope\n\n- The listener\n\n## Acceptance criteria\n\n- [ ] Given a publish, when a loop claims, then the claim does not wait on it.\n\n` +
  "## Verify command(s)\n\n```\npytest tests/test_store.py\n```\n";

const task = (question: string, fields: Partial<ScoutTask>): ScoutTask => ({
  id: 1, pass: 1, wave: 1, question, paths: [], mode: "read", route: "luna", phase: "done",
  started_ms: null, ended_ms: null, duration_ms: null, tokens: 30_000, cost_usd: null, excerpt: null, ...fields,
});

function actions(now: number, filedA1: boolean): ScoutAction[] {
  const item = (position: number, kind: ScoutAction["kind"], title: string, why: string, depends = ""): ScoutAction => ({
    position, pass: 1, title, why, kind, body: draft(title, why, depends), ticket: null, filed_ms: null,
  });
  return [
    { ...item(1, "fix", "Commit the listener’s write before it calls publish", "F1: the open transaction blocks every loop claim for the length of a publish."),
      ...(filedA1 ? { ticket: "DEMO-214", filed_ms: now } : {}) },
    { ...item(2, "test", "A listener publish never blocks a loop claim", "Keeps the wave-1 reproduction as a regression test for F1."), ticket: "DEMO-213", filed_ms: now - 22 * minute },
    item(3, "fix", "Raise the loop’s busy_timeout to 30 s and log each wait over 5 s", "F2: 35 of 37 locks came within 6 s of a publish."),
    item(4, "decide", "Route the digest's writes through the store writer", "F3 is likely, not shown, and one of its places is unverified.", '"DEMO-9"'),
  ];
}

function done(now: number, filedA1: boolean): ScoutDetail {
  const start = now - 60 * minute;
  const at = (minutes: number) => start + minutes * minute;
  const finding = (title: string, status: "shown" | "likely" | "unconfirmed") => ({ title, status, evidence: [] });
  const tasks = [
    task("Where does the loop open SQLite connections?", { started_ms: at(1), ended_ms: at(3), duration_ms: 2 * minute }),
    task("Does the listener write from its own thread?", { started_ms: at(1), ended_ms: at(4), duration_ms: 3 * minute }),
  ];
  const listed = actions(now, filedA1);
  return {
    scout: {
      id: 31, ask: ASK, kind: "bug", phase: "done", reason: "the lead wrote its report", lead: "opus", helper: "luna", swarm: 2,
      tasks_done: 2, tasks_total: 2, created_ms: start - 20 * second, started_ms: start, ended_ms: at(33), elapsed_ms: 33 * minute,
      heartbeat_age_ms: 4000, base_sha: "a41c9e2b7d0f4c3e9a1b2c3d4e5f60718293a4b5", host: "writer",
      budget: { minutes: 30, helper_turns: 4, waves: 1 }, totals: null,
    },
    tasks,
    reports: [{
      pass: 1, at: at(33), report: {
        summary: "Reproduced in a failing test: the comment listener holds the write lock while it publishes, so loop claims time out.",
        findings: [
          finding("The comment listener keeps its write transaction open while it calls publish.", "shown"),
          finding("Loop claims use the default 5 s busy_timeout, shorter than a slow publish.", "shown"),
          finding("The digest job writes through its own connection instead of the store’s writer.", "likely"),
        ],
        actions: listed.map(({ title, why, kind, body }) => ({ title, why, kind, body })),
        reproduction: null, cut: null,
      },
    }],
    actions: listed,
    events: [{ at: at(33), kind: "agent_turn", summary: "scout-lead turn ended" }],
  };
}

/** `/scouts/31` seeded off the browser's clock; filing A1 takes it, filing A4 is refused by the checker. */
async function seed(page: Page) {
  const now = await page.evaluate(() => Date.now());
  let filed = false;
  await page.route(/\/scouts\/31$/, (route) => route.fulfill({ json: done(now, filed) }));
  await page.route(/\/actions\/scout-file$/, async (route) => {
    const { item } = route.request().postDataJSON() as { item: number };
    if (item === 1) {
      filed = true;
      return route.fulfill({ json: { ok: true, ticket: "DEMO-214", problems: [], advisories: [], detail: "filed" } });
    }
    return route.fulfill({ json: { ok: false, ticket: null, problems: [REFUSAL], advisories: [], detail: REFUSAL } });
  });
  await page.route(/\/tickets\/check$/, (route) => route.fulfill({ json: { problems: [REFUSAL], advisories: [], blocks: true } }));
}

async function open(page: Page, scheme: "light" | "dark") {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.emulateMedia({ colorScheme: scheme });
  await page.goto("/next.html");
  await seed(page);
  await page.goto("/next.html#/scouts/demo/31");
  await expect(items(page)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

test.use({ timezoneId: "America/Los_Angeles" });

const items = (page: Page) => page.getByRole("region", { name: "Action items" });

test("01 a done scout's Action items, Dawn", async ({ page }) => {
  await open(page, "light");
  await expect(items(page).getByRole("button", { name: "File" })).toHaveCount(3);
  await expect(items(page).getByText("Filed DEMO-213", { exact: false })).toBeVisible();
  await items(page).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, "01-scout-items-before.png"), fullPage: true });
});

test("02 after filing A1", async ({ page }) => {
  await open(page, "light");
  await items(page).getByRole("listitem").first().getByRole("button", { name: "File" }).click();
  await expect(page.locator("[data-toast]")).toHaveText("Filed DEMO-214 in demo Backlog.");
  await expect(items(page).getByText("2 to file")).toBeVisible();
  await expect(items(page).getByRole("listitem").first()).toContainText("Filed DEMO-214");
  await page.screenshot({ path: path.join(out, "02-scout-items-after.png"), fullPage: true });
});

test("03 a refused item opens New ticket with its draft", async ({ page }) => {
  await open(page, "light");
  await items(page).getByRole("listitem").nth(3).getByRole("button", { name: "File" }).click();
  await expect(page.locator("[data-toast]")).toContainText("The checker refused A4");
  await expect(page.getByRole("textbox", { name: "Title" })).toHaveValue("Route the digest's writes through the store writer");
  await expect(page.getByRole("list", { name: "Checker messages" })).toContainText(REFUSAL);
  await page.screenshot({ path: path.join(out, "03-scout-refused-new-ticket.png") });
});

test("04 the Action items in Night", async ({ page }) => {
  await open(page, "dark");
  await page.screenshot({ path: path.join(out, "04-scout-items-night.png"), fullPage: true });
});
