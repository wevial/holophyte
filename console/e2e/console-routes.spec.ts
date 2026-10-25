import { expect, test } from "@playwright/test";

const sidebar = (page: import("@playwright/test").Page) => page.getByRole("complementary", { name: "Console" });

test("/ opens the console", async ({ page }) => {
  await page.goto("/");
  await expect(sidebar(page).getByRole("link", { name: "Settings" })).toBeVisible();
});

test("/next.html redirects to / and keeps the fragment", async ({ page, baseURL }) => {
  await page.goto("/next.html#/runs?project=demo");
  await expect(page).toHaveURL(new URL("/#/runs?project=demo", baseURL).href);
  await expect(page.getByRole("heading", { name: "Runs", level: 1 })).toBeVisible();
});

test("/classic.html lands on / and the console's sidebar", async ({ page, baseURL }) => {
  await page.goto("/classic.html#view=board");
  await expect(page).toHaveURL((url) => url.origin === new URL(baseURL!).origin && url.pathname === "/");
  await expect(sidebar(page)).toBeVisible();
  await expect(sidebar(page).getByRole("link", { name: "Classic console" })).toHaveCount(0);
});
