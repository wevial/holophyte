import path from "node:path";
import { expect, test } from "@playwright/test";
import hostStatus from "../../tests/fixtures/serve/host-status.json" with { type: "json" };

const out = process.env.CAPTURE_OUT ?? "";
const peer = "spare:8741";

test.use({ video: "on" });

test("Hosts report in night, dawn, unreachable and narrow states", async ({ page }, testInfo) => {
  let failed = false;
  await page.route("**/peers", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, peers: [...(body.peers ?? []), peer] } });
  });
  await page.route(`http://${peer}/**`, async (route) => {
    if (failed) return route.abort("connectionrefused");
    const now = Date.now();
    const json = new URL(route.request().url()).pathname === "/status"
      ? {
        ...hostStatus,
        now,
        projects: [],
        daemon: { ...hostStatus.daemon, started_ms: now - 3 * 86400000 },
        build: { daemon: "9f3a1c2", head: "9f3a1c2", sweep: "9f3a1c2" },
        sweep: { ...hostStatus.sweep, started: now - 180000, ended: now - 120000, projects: {} },
      }
      : { now, level: "none", items: [] };
    await route.fulfill({ json, headers: { "access-control-allow-origin": "*" } });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/next.html#/settings/hosts");
  const writer = page.getByRole("article", { name: "writer-host", exact: true });
  const spare = page.getByRole("article", { name: "spare", exact: true });
  await expect(writer).toBeVisible();
  await expect(spare.getByText("Healthy", { exact: false })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-hosts-night.png"), fullPage: true });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.screenshot({ path: path.join(out, "02-hosts-dawn.png"), fullPage: true });
  failed = true;
  await expect(spare.getByRole("status")).toContainText("spare has not answered since", { timeout: 20000 });
  await expect(spare.getByText("Unknown", { exact: true })).toHaveCount(2);
  await page.screenshot({ path: path.join(out, "03-hosts-unreachable.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(writer).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(out, "04-hosts-390.png"), fullPage: true });
  const video = page.video();
  await page.close();
  if (video) await video.saveAs(testInfo.outputPath("hosts-flow.webm"));
});
