import path from "node:path";
import { expect, test } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";

test("the new console's page and the classic console's ways to and from it", async ({ page }) => {
  await page.goto("/next.html");
  await expect(page.getByText("The new console is being built here. Until it is ready, the classic console does everything.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the classic console" })).toBeVisible();
  await page.screenshot({ path: path.join(out, "01-next-page.png"), fullPage: true });

  await page.goto("/");
  const link = page.getByRole("link", { name: "Try the new console" });
  await expect(link).toBeVisible();
  await page.getByRole("navigation", { name: "Console" }).screenshot({ path: path.join(out, "02-classic-link.png") });

  await page.goto("/#view=board");
  const board = page.getByRole("region", { name: "Board", exact: true });
  await expect(board.getByText("DEMO-2", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Board", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({ path: path.join(out, "03-classic-board-from-link.png"), fullPage: true });
});
