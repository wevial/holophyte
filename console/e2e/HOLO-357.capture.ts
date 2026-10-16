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
const countLine = (page: Page) => page.locator("[data-count-line]");
const filter = (page: Page) => page.getByRole("group", { name: "State" });

async function open(page: Page, hash = "#/runs") {
  await addProject(page, "ledger");
  await page.goto(`/next.html${hash}`);
  await expect(countLine(page)).toContainText(/· [1-9]\d* runs/);
}

test("01 the Runs header and filter on All at 1440 by 900, night and dawn", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await open(page);
  await expect(runs(page).first()).toBeVisible();
  await expect(countLine(page)).toContainText(/^all projects · \d+ runs \/ \d+ active \/ newest first \/ today and yesterday$/);
  await expect(filter(page).getByRole("button", { pressed: true })).toHaveText(/^All \d+$/);
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-runs-header-night.png");

  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await shot(page, "02-runs-header-dawn.png");
});

test("02 the Failed filter pressed with one project picked", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await open(page, "#/runs?project=demo");
  await filter(page).getByRole("button", { name: /^Failed \d+$/ }).click();
  await expect(page).toHaveURL(/state=failed/);
  await expect(page.getByText("Showing demo only")).toBeVisible();
  await expect(filter(page).getByRole("button", { pressed: true })).toHaveText(/^Failed [1-9]\d*$/);
  await expect(runs(page).first()).toBeVisible();
  await expect(runs(page).getByRole("img", { name: "failed" })).toHaveCount(await runs(page).count());
  await shot(page, "03-runs-failed.png");
});

test("03 a filter with no runs says so", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await open(page, "#/runs?project=demo&state=abandoned");
  await expect(filter(page).getByRole("button", { pressed: true })).toHaveText("Abandoned 0");
  await expect(page.getByText("No abandoned runs in demo today or yesterday.")).toBeVisible();
  await shot(page, "04-runs-empty.png");
});

test("04 the header and filter at 390 px wrap without scrolling sideways", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "light" });
  await open(page);
  const buttons = filter(page).getByRole("button");
  const tops = await buttons.evaluateAll((all) => all.map((button) => button.getBoundingClientRect().top));
  expect(new Set(tops).size).toBeGreaterThan(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("heading", { name: "Runs", level: 1 }).evaluate((heading) => heading.scrollIntoView({ block: "start" }));
  await shot(page, "05-runs-header-390.png");
});
