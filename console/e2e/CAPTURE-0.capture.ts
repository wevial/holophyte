import path from "node:path";
import { expect, test } from "@playwright/test";

// The smoke capture: the seeded Board, once DEMO-2's card is on it.
test("board", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Board", exact: true }).click();
  const board = page.getByRole("region", { name: "Board", exact: true });
  await expect(board.getByText("DEMO-2", { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(process.env.CAPTURE_OUT ?? "", "01-board.png"), fullPage: true });
});
