import path from "node:path";
import { expect, test } from "@playwright/test";

// The smoke capture: the new console's seeded Tickets board, once DEMO-2's card is on it.
test("board", async ({ page }) => {
  await page.goto("/#/tickets");
  await expect(page.getByRole("heading", { name: "Tickets", level: 1 })).toBeVisible();
  await expect(page.getByRole("main").getByText("DEMO-2", { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: path.join(process.env.CAPTURE_OUT ?? "", "01-board.png"), fullPage: true });
});
