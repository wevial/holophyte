import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const agent = (page: Page) => page.getByRole("complementary", { name: "Agent", exact: true });

async function runOf(page: Page, ticket: string): Promise<number> {
  const response = await page.request.get("/projects/demo/history?since=0");
  expect(response.ok()).toBe(true);
  const { rows } = await response.json() as { rows: { id: number; ticket: string }[] };
  const ids = rows.filter((row) => row.ticket === ticket).map((row) => row.id);
  expect(ids.length).toBeGreaterThan(0);
  return Math.max(...ids);
}

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await agent(page).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, name) });
}

test("compose a steer, stop, and pull request question", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL, viewport: { width: 1440, height: 1000 }, colorScheme: "light",
    recordVideo: { dir: out, size: { width: 1440, height: 1000 } },
  });
  const page = await context.newPage();
  const live = await runOf(page, "DEMO-7");
  const parked = await runOf(page, "DEMO-8");

  await page.goto(`/next.html#/runs/demo/${live}`);
  await page.getByRole("link", { name: "Steer or ask" }).click();
  const note = page.getByRole("textbox", { name: "Note to the agent" });
  await expect(note).toBeFocused();
  await note.fill("Don't add a flag");
  await expect(agent(page).getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "01-compose-steer.png");

  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await page.getByRole("checkbox", { name: "Stop it now" }).check();
  await expect(agent(page)).toContainText("Stops now and restarts with your note. Nothing is lost.");
  await expect(agent(page).getByRole("button", { name: "Stop and send" })).toBeEnabled();
  await shot(page, "02-compose-stop-dawn.png");

  await page.goto(`/next.html#/runs/demo/${parked}`);
  await agent(page).getByRole("button", { name: "Ask", exact: true }).click();
  await page.getByRole("textbox", { name: "Question for the agent" }).fill("Does it print?");
  await expect(agent(page)).toContainText("The run keeps going.");
  await shot(page, "03-compose-ask.png");

  await page.goto(`/next.html#/runs/demo/${live}`);
  const ask = agent(page).getByRole("button", { name: "Ask", exact: true });
  await expect(ask).toBeDisabled();
  await expect(ask).toHaveAttribute("title", "Questions are answered on the pull request, once the run parks on it.");
  await shot(page, "04-compose-ask-off.png");

  const video = page.video();
  await context.close();
  if (video) await video.saveAs(path.join(out, "compose-flow.webm"));
  if (video) await video.delete();
});
