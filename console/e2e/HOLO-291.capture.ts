import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string, fullPage = false) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage });
}

const daemonRoute = (url: URL) =>
  url.pathname === "/status" || url.pathname === "/attention" || url.pathname.startsWith("/projects/") || url.pathname.startsWith("/ledger");

const needsYou = (page: Page) => page.getByRole("region", { name: "Needs you", exact: true });

test("01 first load: /peers answered, every daemon route held", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.route(daemonRoute, () => new Promise(() => {}));
  await page.goto("/next.html#/");
  await expect(page.getByText(/^Asking .+ for (its|their) projects…$/)).toBeVisible();
  await expect(needsYou(page).getByRole("button", { name: "Needs you" })).toBeVisible();
  await expect(page.locator("[data-count-line]")).toHaveCount(0);
  await shot(page, "01-first-load.png");
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("02 a project's routes failing after a good answer, Dawn and Night", async ({ page }) => {
  test.setTimeout(60_000);
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/");
  await expect(page.locator("[data-count-line]")).toBeVisible();
  await expect(needsYou(page).getByRole("button").nth(1)).toBeVisible();
  await page.route("**/projects/demo/**", (route) => route.fulfill({ status: 503, body: "unavailable" }));
  const banner = page.locator('[data-notice="host-silent"]');
  await expect(banner).toBeVisible({ timeout: 30_000 });
  await expect(banner).toContainText("is not answering.");
  await expect(banner).toContainText("its rows below are from");
  await expect(needsYou(page).getByRole("button").nth(1)).toBeVisible();
  await shot(page, "02-host-down-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "03-host-down-night.png");
});

test("03 the host root's /status says its console is stale", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.route(
    (url) => url.pathname === "/status",
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({
        response,
        json: {
          ...body,
          build: { ...body.build, daemon: "9f3a1c2e7b5d" },
          console: { ...body.console, stale: true, served: "0a1b2c3d4e5f", reason: "console/src changed after the last build" },
        },
      });
    },
  );
  await page.goto("/next.html#/");
  const banner = page.locator('[data-notice="stale-console"]');
  await expect(banner).toContainText("This console is older than writer-host's daemon.");
  await expect(banner).toContainText("console 0a1b2c3 · daemon 9f3a1c2");
  await expect(banner.getByRole("button", { name: "Reload" })).toBeVisible();
  await shot(page, "04-stale-console.png");
});

test("04 the sidebar footer on the seeded host daemon", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/next.html#/");
  const line = page.locator("[data-footer-line]");
  await expect(line).toContainText(/^writer-host · 1 project answering · sweep /);
  await shot(page, "05-footer.png");
});
