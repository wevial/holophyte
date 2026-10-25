import { expect, test } from "@playwright/test";

const sidebar = (page: import("@playwright/test").Page) => page.getByRole("complementary", { name: "Console" });

test("/ opens the new console", async ({ page }) => {
  await page.goto("/");
  await expect(sidebar(page).getByRole("link", { name: "Classic console" })).toBeVisible();
});

test("/next.html redirects to / and keeps the fragment", async ({ page, baseURL }) => {
  await page.goto("/next.html#/runs?project=demo");
  await expect(page).toHaveURL(new URL("/#/runs?project=demo", baseURL).href);
  await expect(page.getByRole("heading", { name: "Runs", level: 1 })).toBeVisible();
});

test("/classic.html opens the classic console, whose New view returns to /", async ({ page, baseURL }) => {
  await page.goto("/classic.html");
  const rail = page.getByRole("navigation", { name: "Console" });
  await expect(rail).toBeVisible();
  await rail.getByRole("link", { name: "New view" }).click();
  await expect(page).toHaveURL(new URL("/", baseURL).href);
  await expect(sidebar(page).getByRole("link", { name: "Classic console" })).toBeVisible();
});
