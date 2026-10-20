import path from "node:path";
import { expect, test, type Page, type Route } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

/** The seeded host daemon with a second project, `relos`, answering as a copy of `demo` with its own findings. */
async function addRelos(page: Page) {
  const host = await (await page.request.get("/status")).json();
  const demo = host.projects.find((project: { name: string | null }) => project.name === "demo");
  const relosPath = demo.path.replace(/demo$/, "relos");
  await page.route(/\/status$/, async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/status") {
      const body = await (await route.fetch()).json();
      return route.fulfill({ json: { ...body, projects: [...body.projects, { ...demo, name: "relos", path: relosPath, runs: [] }] } });
    }
    if (url.pathname === "/projects/relos/status") {
      const body = await (await page.request.get("/projects/demo/status")).json();
      return route.fulfill({ json: { ...body, project: relosPath, runs: [], queued: [] } });
    }
    return route.fallback();
  });
  await page.route(/\/projects\/relos\/attention$/, async (route) => {
    const body = await (await page.request.get("/projects/demo/attention")).json();
    await route.fulfill({ json: { ...body, project: relosPath, level: "none", items: [] } });
  });
  await page.route(/\/projects\/relos\/findings\?/, async (route) => {
    const since = Number(new URL(route.request().url()).searchParams.get("since"));
    const at = since + 3_600_000;
    const finding = (file: string, severity: string, message: string) => ({ path: file, line: 12, severity, message, criterion: null });
    const runOf = (run: number, ticket: string, findings: object[]) => ({
      run, ticket, title: `${ticket} title`, phase: "failed", outcome: "failed", ended_ms: at, pr_url: null,
      rounds: [{ round: 1, started_ms: at, ended_ms: at, verdict: "changes_requested", reviewer_model: "reviewer", findings, instructions: [], operator_notes: [] }],
      ledger: [], advisories: [],
    });
    const followUp = {
      id: 901, run: 41, ticket: "RELOS-41", kind: "feature", text: "**Cache the parsed manifest.** Each request re-reads it.", path: "relos/manifest.py", line: 30,
      state: "pending", created_ms: at, settled_ms: null, filed_as: null, duplicate_of: null, error: null,
    };
    await route.fulfill({
      json: {
        since, limit: 100,
        runs: [
          runOf(41, "RELOS-41", [finding("relos/sync.py", "p1", "**The retry loop never backs off.** It spins on a closed socket.")]),
          runOf(42, "RELOS-42", [finding("relos/cli.py", "p2", "**The flag is read twice.** The second read ignores the default.")]),
        ],
        follow_ups: [followUp],
      },
    });
  });
}

test("01 every project shown: Open and Follow-ups grouped under project sub-headings", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await addRelos(page);
  await page.goto("/next.html#/findings");
  await expect(page.locator("[data-count-line]")).toBeVisible({ timeout: 30_000 });
  const open = page.getByRole("region", { name: "Open" });
  await expect(open.locator("h3").first()).toBeVisible();
  await expect(open.locator("h3")).toHaveCount(2);
  await expect(page.getByRole("region", { name: "Follow-ups" }).locator("h3").first()).toBeVisible();
  await shot(page, "01-findings-by-project.png");
});
