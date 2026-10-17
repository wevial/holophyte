import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

test("save a new ticket and reopen its browser draft", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
    timezoneId: "UTC",
    recordVideo: { dir: out, size: { width: 1440, height: 900 } },
  });
  await context.addInitScript(({ base, token }) => {
    localStorage.setItem(`holophyte.token.${new URL(base).host}`, token);
  }, { base: baseURL!, token: process.env.CONSOLE_TOKEN! });
  const page = await context.newPage();
  await page.clock.setFixedTime(new Date("2026-10-17T14:05:00Z"));
  await page.goto("/next.html#/tickets/demo/new");
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Steer notes show on the run detail card");
  await page.getByRole("textbox", { name: "Summary", exact: true }).fill("The run page lists each steer note.");
  await page.getByRole("group", { name: "Priority", exact: true }).getByRole("button", { name: "P1", exact: true }).click();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.locator("[data-toast]")).toHaveText("Draft kept in this browser.");
  await page.goto("/next.html#/tickets?project=demo");
  await page.getByRole("button", { name: "New ticket", exact: true }).click();
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Title", exact: true })).toHaveValue("Steer notes show on the run detail card");
  await expect(page.getByRole("group", { name: "Priority", exact: true }).getByRole("button", { name: "P1", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("Draft from 14:05 restored.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start over" })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, "01-draft-restored.png"), fullPage: true });
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.locator("[data-toast]")).toHaveText("Draft kept in this browser.");
  await page.screenshot({ path: path.join(out, "02-draft-saved.png"), fullPage: true });
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "draft-flow.webm"));
    await video.delete();
  }
});
