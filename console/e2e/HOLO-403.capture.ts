import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("hold history across themes, reason submission and narrow layout", async ({ browser, baseURL }) => {
  execFileSync("python3", ["-c", `
import os
from pathlib import Path
from holophyte.config.project import Project
from holophyte.loop.runs import open_store
from holophyte.admission import project_of
from store.operate import record_project_intervention

target = Project.locate(Path(os.environ["HOLOPHYTE_HOME"]) / "demo", adopt=False)
with target.config_path.open("a") as config:
    config.write("\\n[loop]\\nworkers = 2\\n")
conn = open_store(target)
project = project_of(conn, target)
record_project_intervention(conn, "hold", "Holding for the schema bump.", project_id=project, now=1791553200000, actor="supervisor")
record_project_intervention(conn, "release_hold", "Schema bump is out; back to normal.", project_id=project, now=1791568920000, actor="maintainer")
conn.close()
`], { cwd: fileURLToPath(new URL("../..", import.meta.url)) });
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
    timezoneId: "UTC",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } },
  });
  await context.addInitScript(({ base, token }) => {
    localStorage.setItem(`holophyte.token.${new URL(base).host}`, token);
  }, { base: process.env.CONSOLE_URL!, token: process.env.CONSOLE_TOKEN! });
  const page = await context.newPage();
  await page.goto("/next.html#/settings/hold?project=demo");
  await expect(page.getByText("Claims Ready tickets as its 2 workers free up.")).toBeVisible();
  await expect(page.getByText("Schema bump is out; back to normal.")).toBeVisible();
  await shot(page, "01-hold-dawn.png");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await shot(page, "02-hold-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await page.getByRole("button", { name: "Held", exact: true }).click();
  await expect(page.getByRole("button", { name: "Hold demo", exact: true })).toBeDisabled();
  await page.getByPlaceholder("Why it should stop taking work").fill("Waiting on the migration");
  await shot(page, "03-hold-reason.png");
  await page.getByRole("button", { name: "Hold demo", exact: true }).click();
  await expect(page.getByText("Held demo. Nothing new is claimed.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Release", exact: true })).toBeVisible();
  await expect(page.getByText(/Held since .* Runs in flight finish; nothing new is claimed\./)).toBeVisible();
  await shot(page, "04-hold-held.png");
  await page.setViewportSize({ width: 390, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "05-hold-390.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "hold-flow.webm"));
    await video.delete();
  }
});
