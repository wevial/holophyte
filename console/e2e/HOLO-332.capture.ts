import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

/** A second project, `ledger`, answering with the seeded `demo`'s routes. */
async function addProject(page: Page, name: string) {
  await page.route("**/status", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/status") return route.fallback();
    const response = await route.fetch();
    const body = await response.json();
    const demo = body.projects.find((project: { name: string | null }) => project.name === "demo");
    const copy = { ...demo, name, path: demo.path.replace(/demo$/, name) };
    await route.fulfill({ response, json: { ...body, projects: [...body.projects, copy] } });
  });
  await page.route(new RegExp(`/projects/${name}/`), async (route) => {
    const url = route.request().url().replace(`/projects/${name}/`, "/projects/demo/");
    const response = await route.fetch({ url });
    if (!new URL(url).pathname.endsWith("/status")) return route.fulfill({ response });
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, project: body.project.replace(/demo$/, name) } });
  });
}

const runs = (page: Page) => page.getByRole("list", { name: "Runs, newest first" }).getByRole("listitem");

async function open(page: Page, hash = "#/runs") {
  await addProject(page, "ledger");
  await page.goto(`/next.html${hash}`);
  await expect(runs(page).first()).toBeVisible();
  for (const word of ["running", "on CI", "parked", "failed", "merged"]) {
    await expect(runs(page).getByRole("img", { name: word }).first()).toBeVisible();
  }
}

test("01 the Runs table at 1440 by 900, night and dawn", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await open(page);
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-runs-night.png");

  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await shot(page, "02-runs-dawn.png");
});

test("02 the Runs table with one project picked in the switcher", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await open(page);
  const all = await runs(page).count();
  await page.getByRole("button", { name: /Switch project$/ }).click();
  const dialog = page.getByRole("dialog", { name: "Switch project" });
  await dialog.getByRole("combobox").fill("demo");
  await dialog.getByRole("combobox").press("Enter");
  await expect(page).toHaveURL(/project=demo/);
  await page.goto("/next.html#/runs?project=demo");
  await expect(runs(page)).toHaveCount(all / 2);
  await expect(page.getByRole("link", { name: /^Run \d+, .*, ledger, / })).toHaveCount(0);
  await shot(page, "03-runs-scoped.png");
});

test("03 the Runs table at 390 px scrolls inside its box, not the page", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "light" });
  await open(page);
  const box = page.locator("[data-run-table]");
  await box.evaluate((element) => element.scrollBy({ left: 200 }));
  expect(await box.evaluate((element) => element.scrollWidth > element.clientWidth && element.scrollLeft > 0)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("heading", { name: "Runs", level: 1 }).evaluate((heading) => heading.scrollIntoView({ block: "start" }));
  await shot(page, "04-runs-390.png");
});
