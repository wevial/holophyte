import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

async function shot(page: Page, name: string, fullPage = true) {
  await page.evaluate(() => document.fonts.ready);
  const style = fullPage ? await page.addStyleTag({ content: "main { overflow: visible !important; } #root > div { height: auto !important; overflow: visible !important; }" }) : null;
  await page.screenshot({ path: path.join(out, name), fullPage });
  await style?.evaluate(element => element.remove());
}

const box = (page: Page, name: string) => page.getByRole("textbox", { name, exact: true });

test("header fields on New ticket in both themes, an Evidence problem on its row, and Edit ticket at 390 px", async ({ browser, baseURL }) => {
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
  await page.goto("/next.html#/tickets/demo/new");
  await box(page, "Title").fill("Export the orders list as a sheet");
  await box(page, "Summary").fill("Add an Export button that downloads the orders list as a CSV sheet.");
  await box(page, "What").fill("An Export button on the orders page.");
  await box(page, "Why").fill("Ops needs orders in spreadsheets without database access.");
  await box(page, "How").fill("Stream the orders query through the csv module.");
  await box(page, "AC1").fill("Given three orders, when Export is pressed, then a sheet of four lines downloads.");
  const ui = page.getByRole("combobox", { name: "UI change", exact: true });
  const radius = page.getByRole("combobox", { name: "Blast radius", exact: true });
  await expect(ui.locator("option")).toHaveText(["None", "Minor", "Major"]);
  await ui.selectOption({ label: "Minor" });
  await radius.selectOption({ label: "High" });
  await page.getByRole("button", { name: "+ Add a state", exact: true }).click();
  await page.getByRole("button", { name: "+ Add a state", exact: true }).click();
  await expect(box(page, "E1 File")).toHaveValue("01-");
  await expect(box(page, "E2 File")).toHaveValue("02-");
  await box(page, "E1 File").fill("01-export-button.png");
  await box(page, "E1 State").fill("the orders page with its Export button");
  await box(page, "E2 File").fill("02-export-done.png");
  await box(page, "E2 State").fill("the toast after the sheet downloads");
  await expect(page.getByText("2 of 6", { exact: true })).toBeVisible();
  const checker = page.getByRole("region", { name: "Checker" });
  await expect(checker.getByText("Checking…")).toBeHidden();
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "01-new-ticket-fields-dawn.png");

  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "02-new-ticket-fields-night.png");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();

  await box(page, "E2 File").fill("shot.png");
  const problem = page.getByText('header evidence[2].file must read 02-slug.png or 02-slug.webm, not "shot.png"');
  await expect(problem.first()).toBeVisible();
  await expect(box(page, "E2 File")).toHaveAttribute("aria-invalid", "true");
  await expect(box(page, "E1 File")).not.toHaveAttribute("aria-invalid", "true");
  await box(page, "E2 File").evaluate(element => element.scrollIntoView({ block: "center" }));
  await shot(page, "03-new-ticket-evidence-problem.png", false);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/next.html#/tickets/demo/DEMO-14/edit");
  await expect(page.getByRole("combobox", { name: "Blast radius", exact: true })).toHaveValue("medium");
  await expect(page.getByRole("combobox", { name: "UI change", exact: true })).toHaveValue("minor");
  await expect(box(page, "E2 File")).toHaveValue("02-export-done.png");
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot(page, "04-edit-ticket-fields-390.png");

  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "header-fields-flow.webm"));
    await video.delete();
  }
});
