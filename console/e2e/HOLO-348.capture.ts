import path from "node:path";
import { expect, test, type Page, type Route } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const FLAG = {
  path: "holophyte/serve/server.py", line: 12, severity: "p2", criterion: null,
  message: "**Flag is ignored** The `--dry-run` flag never reaches the route, so a dry run writes.",
};
const THREAD = "https://github.com/example/demo/pull/43#discussion_r3: Consider a retry cap on the board sync too.";

/** `/findings` as seeded, DEMO-11's failed run given an open P2 finding and DEMO-10's merged run a bot thread. */
async function findings(route: Route) {
  const response = await route.fetch();
  const body = await response.json();
  const base = route.request().url().replace(/\/findings\?.*$/, "");
  const runs = await (await route.fetch({ url: `${base}/runs` })).json();
  const list = (runs.runs ?? runs.rows ?? runs) as { id?: number; run?: number; ticket: string; ended_ms?: number }[];
  const failed = list.find((run) => run.ticket === "DEMO-11")!;
  const ended = failed.ended_ms ?? Date.now() - 50 * 60_000;
  const demo11 = {
    run: failed.id ?? failed.run, ticket: "DEMO-11", title: "Retry the board sync", phase: "failed", outcome: "failed",
    ended_ms: ended, pr_url: null, ledger: [], advisories: [],
    rounds: [{ round: 1, started_ms: ended - 20 * 60_000, ended_ms: ended - 15 * 60_000, verdict: "changes_requested",
      reviewer_model: "reviewer-model", findings: [FLAG], instructions: [], operator_notes: [] }],
  };
  const seeded = (body.runs as { ticket: string; advisories: unknown[] }[]).map((run) =>
    run.ticket === "DEMO-10" ? { ...run, advisories: [{ tone: "advisory", message: THREAD }] } : run);
  await route.fulfill({ response, json: { ...body, runs: [...seeded, demo11] } });
}

async function shot(page: Page, name: string) {
  await page.mouse.move(1, 1);
  await page.waitForTimeout(500);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

const rowOf = (page: Page, group: string, text: string) =>
  page.getByRole("region", { name: group }).locator("[data-finding-row]").filter({ hasText: text }).first();

async function unfold(page: Page, group: string, text: string) {
  const row = rowOf(page, group, text);
  await row.locator("button[aria-expanded]").click();
  await row.scrollIntoViewIfNeeded();
  return row;
}

async function open(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.route("**/findings?*", findings);
  await page.goto("/next.html#/findings");
  await expect(page.locator("[data-count-line]")).toBeVisible({ timeout: 30_000 });
  await expect(rowOf(page, "Open", "Flag is ignored")).toBeVisible();
}

test("01 DEMO-11's failed row unfolded with Steer into next run, Dawn and Night", async ({ page }) => {
  await open(page);
  const row = await unfold(page, "Open", "Flag is ignored");
  await expect(row.getByRole("button", { name: "Steer into next run" })).toBeEnabled();
  await shot(page, "01-steer-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-steer-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
});

test("02 the same row after the steer, its stamped fate line and the toast", async ({ page }) => {
  await page.route("**/actions/steer", (route) =>
    route.fulfill({ json: { action: "steer", ok: true, ticket: "DEMO-11", recorded: 1, detail: "DEMO-11 steered: carried into its next run" } }));
  await open(page);
  const row = await unfold(page, "Open", "Flag is ignored");
  await row.getByRole("button", { name: "Steer into next run" }).click();
  await expect(row.locator("[data-fate]")).toHaveText("steered · DEMO-11 reads it on its next run");
  await expect(page.locator("[data-toast]")).toHaveText("Steered DEMO-11 with the finding.");
  await shot(page, "03-steered.png");
});

test("03 an advisory row after File ticket", async ({ page }) => {
  await page.route("**/tickets", (route) =>
    route.request().method() === "POST" ? route.fulfill({ status: 201, json: { ticket: "HOLO-248", revision: 1 } }) : route.fallback());
  await open(page);
  const row = await unfold(page, "Follow-ups", "Consider a retry cap");
  await row.getByRole("button", { name: "File ticket" }).click();
  await expect(row.locator("[data-fate]")).toHaveText("filed as HOLO-248 · Backlog");
  await expect(page.locator("[data-toast]")).toHaveText("Filed HOLO-248 in Backlog.");
  await shot(page, "04-filed.png");
});
