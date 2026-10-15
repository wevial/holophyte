import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function addHeldProjects(page: Page, names: string[]) {
  await page.route("**/status", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/status") return route.fallback();
    const response = await route.fetch();
    const body = await response.json();
    const demo = body.projects.find((project: { name: string | null }) => project.name === "demo");
    const copies = names.map((name) => ({ ...demo, name, path: demo.path.replace(/demo$/, name), admission: "held" }));
    await route.fulfill({ response, json: { ...body, projects: [...body.projects, ...copies] } });
  });
  await page.route(/\/projects\/(?!demo\/)[^/]+\//, async (route) => {
    const name = new URL(route.request().url()).pathname.split("/")[2]!;
    const url = route.request().url().replace(`/projects/${name}/`, "/projects/demo/");
    const response = await route.fetch({ url });
    if (!url.endsWith("/status")) return route.fulfill({ response });
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, project: body.project.replace(/demo$/, name), admission: "held" } });
  });
}

const switcher = (page: Page) => page.getByRole("button", { name: /Switch project$/ });
const dialog = (page: Page) => page.getByRole("dialog", { name: "Switch project" });

test("the project switcher: open, filtered, scoped and on the folded rail", async ({ page }) => {
  await addHeldProjects(page, ["ledger"]);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/next.html");
  await expect(page.getByText(/ · polled /)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);

  await switcher(page).click();
  await expect(dialog(page).getByRole("option")).toHaveCount(3);
  await expect(dialog(page).getByRole("option", { name: /ledger/ })).toContainText("held");
  await page.screenshot({ path: path.join(out, "01-switcher-open.png") });

  await dialog(page).getByRole("combobox").fill("led");
  await expect(dialog(page).getByRole("option")).toHaveCount(1);
  await page.screenshot({ path: path.join(out, "02-switcher-filtered.png") });

  await dialog(page).getByRole("combobox").fill("dem");
  await dialog(page).getByRole("combobox").press("Enter");
  await expect(dialog(page)).toBeHidden();
  await expect(page).toHaveURL(/#\/\?project=demo$/);
  await expect(switcher(page)).toHaveAccessibleName(/^Project: demo/);
  await page.screenshot({ path: path.join(out, "03-switcher-scoped.png") });

  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await switcher(page).click();
  await expect(dialog(page)).toBeVisible();
  await page.screenshot({ path: path.join(out, "04-switcher-rail.png") });
});

test("the folded switcher's dialog stays inside a 390 px viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/next.html");
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await switcher(page).click();
  const box = (await dialog(page).boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
});

test("arrow keys scroll the active option into view in a long list", async ({ page }) => {
  await addHeldProjects(page, Array.from({ length: 14 }, (_, at) => `held-${at + 1}`));
  await page.goto("/next.html");
  await switcher(page).focus();
  await page.keyboard.press("ArrowDown");
  await expect(dialog(page).getByRole("option")).toHaveCount(16);
  await page.keyboard.press("ArrowUp");
  await expect(dialog(page).getByRole("option", { name: /held-14/ })).toHaveAttribute("data-active", "true");
  await expect(dialog(page).getByRole("option", { name: /held-14/ })).toBeInViewport({ ratio: 1 });
  await expect.poll(() => dialog(page).getByRole("listbox").evaluate((list) => list.scrollTop)).toBeGreaterThan(0);
});

test("on a short viewport the dialog's list ends on screen and reaches its last option", async ({ page }) => {
  await addHeldProjects(page, Array.from({ length: 14 }, (_, at) => `held-${at + 1}`));
  await page.setViewportSize({ width: 390, height: 320 });
  await page.goto("/next.html");
  await switcher(page).focus();
  await page.keyboard.press("ArrowDown");
  await expect(dialog(page).getByRole("option")).toHaveCount(16);
  const box = (await dialog(page).boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(320);
  await page.keyboard.press("ArrowUp");
  await expect(dialog(page).getByRole("option", { name: /held-14/ })).toBeInViewport({ ratio: 1 });
});
