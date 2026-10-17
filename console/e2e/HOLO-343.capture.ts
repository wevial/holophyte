import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name) });
}

async function runOf(page: Page, ticket: string): Promise<number> {
  const response = await page.request.get("/projects/demo/history?since=0");
  expect(response.ok()).toBe(true);
  const { rows } = await response.json() as { rows: { id: number; ticket: string }[] };
  const ids = rows.filter((row) => row.ticket === ticket).map((row) => row.id);
  expect(ids.length).toBeGreaterThan(0);
  return Math.max(...ids);
}

test("Run actions and reasoned Abort in Night and Dawn, Requeue, and actions off", async ({ page }, testInfo) => {
  const live = await runOf(page, "DEMO-7");
  const failed = await runOf(page, "DEMO-11");
  await page.goto(`/next.html#/runs/demo/${live}`);
  const actions = page.locator("[data-run-actions]");
  await expect(actions.getByRole("button", { name: "Pause", exact: true })).toBeEnabled();
  await expect(actions.getByRole("button", { name: "Abort", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-run-actions-live.png");
  await actions.getByRole("button", { name: "Abort", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: `Abort run ${live}?` });
  await expect(dialog.getByRole("button", { name: "Keep running" })).toBeFocused();
  await expect(dialog.getByRole("button", { name: "Abort run", exact: true })).toBeDisabled();
  await dialog.getByRole("textbox", { name: "Reason · saved with the run" }).fill("wrong ticket");
  await expect(dialog.getByRole("button", { name: "Abort run", exact: true })).toBeEnabled();
  await shot(page, "02-run-abort-confirm.png");
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await actions.getByRole("button", { name: "Abort", exact: true }).click();
  await dialog.getByRole("textbox", { name: "Reason · saved with the run" }).fill("wrong ticket");
  await shot(page, "03-run-abort-confirm-dawn.png");
  await dialog.getByRole("button", { name: "Keep running" }).click();
  await page.goto(`/next.html#/runs/demo/${failed}`);
  await expect(actions.getByRole("button", { name: "Requeue", exact: true })).toBeEnabled();
  await expect(actions.getByRole("button")).toHaveCount(1);
  await shot(page, "04-run-requeue.png");
  await page.route(/\/projects\/demo\/status$/, async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), actions: false } });
  });
  await page.goto(`/next.html#/runs/demo/${live}`);
  await page.reload();
  for (const name of ["Pause", "Abort"]) {
    const button = actions.getByRole("button", { name, exact: true });
    await expect(button).toBeDisabled();
    await expect(button).toHaveAttribute("title", "Actions are off on this host. [serve] actions = true turns them on.");
  }
  await shot(page, "05-run-actions-off.png");
  const video = page.video();
  if (video) {
    await page.close();
    await video.saveAs(testInfo.outputPath("run-actions.webm"));
  }
});
