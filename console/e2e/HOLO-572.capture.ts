import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("Run page Abort dialog with Close PR", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
  const page = await context.newPage();
  await page.route("**/status", async route => {
    if (new URL(route.request().url()).pathname !== "/status") return route.continue();
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), actions: true } });
  });
  const status = await (await page.request.get("/status")).json();
  const project = status.projects[0];
  let live: { id: number } | undefined;
  let detail: { run: { pr_url: string | null } } | undefined;
  for (const run of project.runs as { id: number; phase: string; stop_requested?: unknown }[]) {
    if (run.phase !== "working" || run.stop_requested != null) continue;
    const answer = await page.request.get(`/projects/${project.name}/runs/${run.id}`);
    const body = await answer.json();
    if (body.run?.pr_url != null) [live, detail] = [run, body];
  }
  if (live == null || detail == null) throw new Error("no live run with a pull request in the fixture");
  const number = /\/pull\/(\d+)/.exec(detail.run.pr_url!)![1];
  await page.goto(`/next.html#/runs/${project.name}/${live.id}`);
  await page.locator("[data-run-actions]").getByRole("button", { name: "Abort", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: `Abort run ${live.id}?` });
  await dialog.getByRole("textbox").fill("superseded by a smaller ticket");
  const box = dialog.getByRole("checkbox", { name: `Close PR #${number}` });
  await expect(box).not.toBeChecked();
  await expect(dialog).toContainText(`and PR #${number} are kept`);
  await expect(dialog.getByRole("button", { name: "Abort run", exact: true })).toBeVisible();
  await shot(page, "01-run-abort-close-unticked.png");
  await box.check();
  await expect(dialog).toContainText(`is kept, PR #${number} is closed`);
  await expect(dialog.getByRole("button", { name: `Abort run and close PR #${number}`, exact: true })).toBeVisible();
  await shot(page, "02-run-abort-close-ticked.png");
  await context.close();
});
