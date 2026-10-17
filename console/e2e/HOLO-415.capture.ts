import path from "node:path";
import { expect, test } from "@playwright/test";
import host from "../../tests/fixtures/serve/host-status.json" with { type: "json" };
import status from "../../tests/fixtures/serve/status.json" with { type: "json" };

const out = process.env.CAPTURE_OUT ?? "";
test.use({ video: "on" });

test("agent routes in night, dawn, unreachable and narrow states", async ({ page }, testInfo) => {
  let failed = false;
  const now = new Date(2026, 9, 16, 16, 39).getTime();
  await page.clock.setFixedTime(now);
  await page.route("**/peers", (route) => route.fulfill({ json: { self: "writer-host:8741", peers: [] } }));
  await page.route("**/attention", (route) => route.fulfill({ json: { now, level: "none", items: [], project: "/root/alpha" } }));
  await page.route("**/status", (route) => {
    if (failed) return route.abort("connectionrefused");
    const project = new URL(route.request().url()).pathname.includes("/projects/");
    const json = project ? {
      ...status, now, host: "writer-host", project: "/root/alpha", runs: [], queued: [],
      route_labels: { ...status.route_labels, implementer_fallback: "claude sonnet", reviewer: "codex gpt-6-sol", reviewer_fallback: "claude opus" },
      active_routes: { ...status.active_routes, reviewer: { command: null, fallback: "claude" } },
      route_probes: { ...status.route_probes,
        implementer: { primary: { ok: true, at_ms: now - 41000, age_ms: 41000, duration_ms: 1800, why: null }, fallback: null },
        reviewer: { primary: { ok: false, at_ms: now - 180000, age_ms: 180000, duration_ms: 900, why: "exit 1" },
          fallback: { ok: true, at_ms: now - 179000, age_ms: 179000, duration_ms: 1300, why: null } },
      },
    } : { ...host, now, projects: [{ ...host.projects[0], host: "writer-host", runs: [] }],
      daemon: { ...host.daemon, started_ms: now - 3 * 86400000 },
      sweep: { ...host.sweep, state: "fresh", ended: now - 120000 },
    };
    return route.fulfill({ json });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/next.html#/settings/hosts");
  const card = page.getByRole("article", { name: "writer-host", exact: true });
  await expect(card.getByText("1 route down · reviewer on its fallback")).toBeVisible();
  await expect(card.getByText("in use · recorded")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-routes-night.png"), fullPage: true });
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.screenshot({ path: path.join(out, "02-routes-dawn.png"), fullPage: true });
  failed = true;
  await expect(card.getByText("as of 16:39", { exact: true })).toBeVisible({ timeout: 20000 });
  await expect(card.getByText("unknown · ok at 16:38 · not probed since")).toBeVisible();
  await card.getByRole("table").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, "03-routes-unreachable.png"), fullPage: true });
  failed = false;
  await expect(card.getByText("1 route down · reviewer on its fallback")).toBeVisible({ timeout: 20000 });
  await page.setViewportSize({ width: 390, height: 844 });
  await card.getByRole("table").scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(out, "04-routes-390.png"), fullPage: true });
  const video = page.video();
  await page.close();
  if (video) await video.saveAs(testInfo.outputPath("routes-flow.webm"));
});
