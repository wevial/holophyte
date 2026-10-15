import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const MINUTE = 60_000;

const queuedTicket = (ticket: string, title: string, reason: string, pr_url: string | null, waiting_ms: number) => ({
  ticket, ticket_url: null, title, reason, pr_url, waiting_ms,
});

async function queueAndQuiet(page: Page) {
  await page.route("**/projects/demo/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const template = body.runs[0];
    const quiet = {
      ...template, id: 912, ticket: "DEMO-12", title: "Retry the flaky check",
      heartbeat_age_ms: body.thresholds.heartbeat_stale_ms + 9 * MINUTE,
    };
    const queued = [
      queuedTicket("DEMO-2", "Name the held project", "approved for merge\nthe next claim resumes the candidate at the merge gate",
        "https://github.com/example/demo/pull/41", 12 * MINUTE + 4_000),
      queuedTicket("DEMO-13", "Show the lane's queue", "waiting for a free worker", null, 5 * MINUTE),
      queuedTicket("DEMO-14", "Count the queued tickets", "waiting for a free worker", null, 2 * MINUTE),
    ];
    await route.fulfill({ response, json: { ...body, runs: [...body.runs, quiet], queued } });
  });
}

const running = (page: Page) => page.getByRole("region", { name: "Running", exact: true });

test("the new Overview's Running section: a lane per project, live rows, a quiet run and the queue", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/");
  const lane = running(page).getByRole("group", { name: "demo" });
  await expect(lane.getByRole("link", { name: /^DEMO-3 / })).toBeVisible();
  await expect(lane.getByRole("link", { name: /^DEMO-7 / })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-running-dawn.png") });

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(out, "02-running-night.png") });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  const queue = await page.context().newPage();
  await queueAndQuiet(queue);
  await queue.setViewportSize({ width: 1440, height: 900 });
  await queue.goto("/next.html#/");
  const queueLane = running(queue).getByRole("group", { name: "demo" });
  await expect(queueLane).toContainText("DEMO-12 went quiet. It is up in Needs you.");
  await queueLane.getByRole("button", { name: "3 queued" }).click();
  await expect(queueLane.getByRole("button", { name: "3 queued" })).toHaveAttribute("aria-expanded", "true");
  await expect(queueLane.getByRole("list", { name: "demo queued" }).getByRole("listitem")).toHaveCount(3);
  await queue.evaluate(() => document.fonts.ready);
  await running(queue).screenshot({ path: path.join(out, "03-running-queue-open.png") });
  await queue.close();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(lane.getByRole("link", { name: /^DEMO-7 / })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await running(page).screenshot({ path: path.join(out, "04-running-390.png") });
});
