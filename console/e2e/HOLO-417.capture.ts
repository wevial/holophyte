import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("daemon restart confirmation and watched restart", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 },
    colorScheme: "dark", recordVideo: { dir: out, size: { width: 1440, height: 900 } } });
  const page = await context.newPage();
  let restarting = false;
  let started = 1000;
  await page.route("**/status", async route => {
    if (new URL(route.request().url()).pathname !== "/status") return route.continue();
    if (restarting) return route.abort("connectionrefused");
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, actions: true,
      daemon: { ...body.daemon, started_ms: started },
      build: { ...body.build, head: "0a1b2c3d4567890" },
      projects: body.projects.map((project: { runs: unknown[] }, index: number) => ({
        ...project,
        runs: index === 0 ? [
          { id: 1, ticket: "DEMO-1", phase: "working", heartbeat_age_ms: 1000 },
          { id: 2, ticket: "DEMO-2", phase: "working", heartbeat_age_ms: 1000 },
          { id: 3, ticket: "DEMO-3", phase: "reviewing", heartbeat_age_ms: 1000 },
        ] : [],
      })),
    } });
  });
  await page.route("**/actions/restart-daemon", async route => {
    restarting = true;
    await route.fulfill({ json: { ok: true, detail: "Restart recorded" } });
  });
  await page.goto("/next.html#/settings/hosts");
  const card = page.getByRole("article", { name: "writer-host", exact: true });
  const dialog = page.getByRole("dialog", { name: "Restart writer-host", exact: true });
  await card.getByRole("button", { name: "Restart daemon", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Keep running" })).toBeFocused();
  await expect(dialog).toContainText("3 runs in flight keep going: they run in the loop, not in this daemon");
  await shot(page, "01-restart-confirm-night.png");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await card.getByRole("button", { name: "Restart daemon", exact: true }).click();
  await expect(dialog).toBeVisible();
  await shot(page, "02-restart-confirm-dawn.png");
  await dialog.getByRole("button", { name: "Restart daemon", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(card).toContainText("Restarting · back in about 20s");
  await expect(card.getByRole("status")).toHaveText("Restarting the daemon. Runs keep going; this card comes back when it answers.");
  await expect(card.getByRole("button", { name: "Run sweep", exact: true })).toBeDisabled();
  await expect(card.getByRole("button", { name: "Restart daemon", exact: true })).toBeDisabled();
  await shot(page, "03-restarting.png");
  started = 2000;
  restarting = false;
  await expect(page.getByText("Restarted the daemon on writer-host.", { exact: true })).toBeVisible({ timeout: 20000 });
  await page.setViewportSize({ width: 390, height: 900 });
  await card.getByRole("button", { name: "Restart daemon", exact: true }).click();
  await expect(dialog).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "04-restart-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "restart-flow.webm"));
    await video.delete();
  }
});
