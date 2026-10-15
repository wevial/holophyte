import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

const ready = (run: number) => ({
  run, ticket: "DEMO-9", pr_url: "https://github.com/example/demo/pull/42",
  head_sha: "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f", ready: true, reason: null, detail: null,
  facts: [{ name: "parked", ok: true, detail: `run ${run} is DEMO-9's newest run, parked awaiting_merge_approval` }],
});

async function readyToMerge(page: Page) {
  await page.route("**/runs/*/merge", (route) => {
    const run = Number(new URL(route.request().url()).pathname.split("/").at(-2));
    return route.fulfill({ json: ready(run) });
  });
}

const queuedTicket = (ticket: string) => ({
  ticket, ticket_url: null, title: ticket, reason: "waiting for a free worker", pr_url: null, waiting_ms: 5 * 60_000,
});

async function emptied(page: Page) {
  await page.route("**/attention", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, level: "none", items: [] } });
  });
  await page.route("**/projects/demo/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const queued = body.queued.length >= 2 ? body.queued.slice(0, 2) : [queuedTicket("DEMO-2"), queuedTicket("DEMO-12")];
    await route.fulfill({ response, json: { ...body, runs: [], queued } });
  });
  await page.route("**/projects/demo/shipped?*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, rows: [], next_before: null } });
  });
}

const heading = (page: Page) => page.getByRole("heading", { level: 1 });
const label = (page: Page, name: string) =>
  page.getByRole("region", { name, exact: true }).getByRole("button", { name, exact: true });

test("the new console's Overview: header, count line, four folding sections and their empty sentences", async ({ page }) => {
  await readyToMerge(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/");
  await expect(heading(page)).toHaveText(/^(\d+ needs? you\.|Nothing needs you\.)$/);
  await expect(page.locator("[data-count-line]")).toHaveText(/ merged today$/);
  await expect(page.getByRole("region", { name: "Pull requests" })).toContainText(/waits? on you/);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-overview-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-overview-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  for (const name of ["Running", "Merged today"]) {
    await label(page, name).click();
    await expect(label(page, name)).toHaveAttribute("aria-expanded", "false");
  }
  await page.screenshot({ path: path.join(out, "03-overview-folded.png") });
  for (const name of ["Running", "Merged today"]) await label(page, name).click();

  const empty = await page.context().newPage();
  await emptied(empty);
  await empty.goto("/next.html#/");
  await expect(heading(empty)).toHaveText("Nothing needs you.");
  await expect(empty.getByText("The factory has it from here.")).toBeVisible();
  await expect(empty.getByText(/^Nothing running\. \S+ and \S+ are queued\.$/)).toBeVisible();
  await expect(empty.getByText("No pull requests open.")).toBeVisible();
  await expect(empty.getByText("Nothing merged yet today.")).toBeVisible();
  await empty.evaluate(() => document.fonts.ready);
  await empty.screenshot({ path: path.join(out, "04-overview-empty.png") });
  await empty.close();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(heading(page)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: path.join(out, "05-overview-390.png"), fullPage: true });
});
