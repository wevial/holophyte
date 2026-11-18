import { expect, test, type Locator, type Page } from "@playwright/test";

for (const textSize of [110, 120]) {
  test(`at 375px and ${textSize}% text, Custom range opens its dates menu inside the viewport`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.addInitScript((size) => localStorage.setItem("holophyte.textSize", size), String(textSize));
    await page.goto("/#/findings?from=2026-10-01&to=2026-10-15");
    await page.getByRole("button", { name: "Custom range" }).click();
    const menu = page.getByRole("group", { name: "Range range" });
    await expect(menu.getByLabel("From")).toHaveValue("2026-10-01");
    const box = (await menu.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
    await expect(page.getByText("Something went wrong")).toHaveCount(0);
  });
}

test("at 320px and 120% text with a custom range picked, the range presets and Custom range wrap to stay inside the viewport", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 812 });
  await page.addInitScript(() => localStorage.setItem("holophyte.textSize", "120"));
  await page.goto("/#/findings?from=2026-10-01&to=2026-10-15");
  const custom = page.getByRole("button", { name: "Custom range" });
  await expect(custom).toHaveText("Oct 1 – Oct 15");
  for (const button of [...(await page.getByRole("group", { name: "Range" }).getByRole("button").all()), custom]) {
    const box = (await button.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(320);
  }
});

/** Every corner of the dates menu and of its Done button, 10px in past their rounding, is hit-tested to the menu: nothing clips or covers either. */
async function expectShown(menu: Locator) {
  await expect(async () => {
    const hidden = await menu.evaluate((node) => {
      const done = [...node.querySelectorAll("button")].find((button) => button.textContent === "Done")!;
      return [node, done].flatMap((part) => {
        const box = part.getBoundingClientRect();
        const corners = [[box.left + 10, box.top + 10], [box.right - 10, box.top + 10], [box.left + 10, box.bottom - 10], [box.right - 10, box.bottom - 10]];
        return corners.filter(([x, y]) => !node.contains(document.elementFromPoint(x!, y!))).map(([x, y]) => `${part === node ? "menu" : "Done"} ${x},${y}`);
      });
    });
    expect(hidden).toEqual([]);
  }).toPass({ timeout: 2000 });
}

async function openFolded(page: Page, width: number, height: number, hash = "#/findings") {
  await page.setViewportSize({ width, height });
  await page.addInitScript(() => {
    localStorage.setItem("holophyte.textSize", "125");
    localStorage.setItem("holophyte.next.rail", "folded");
  });
  await page.goto(`/${hash}`);
  await expect(page.locator("[data-count-line]")).toBeVisible();
}

async function toggleSidebar(page: Page, name: "Expand sidebar" | "Collapse sidebar") {
  await expect(page.getByRole("button", { name })).toBeVisible();
  await page.keyboard.press("Control+Backslash");
  await expect(page.getByRole("button", { name })).toHaveCount(0);
}

const rangeMenu = (page: Page) => page.getByRole("group", { name: "Range range" });

test("at 720x425 and 125% text, expanding the folded sidebar by shortcut with Custom range open keeps the dates menu and Done shown", async ({ page }) => {
  await openFolded(page, 720, 425);
  await page.getByRole("button", { name: "Custom range" }).click();
  await toggleSidebar(page, "Expand sidebar");
  await expectShown(rangeMenu(page));
});

test("at 720x425 and 125% text, scrolling Findings down after the sidebar expands under an open Custom range keeps the dates menu and Done shown", async ({ page }) => {
  await openFolded(page, 720, 425);
  await page.getByRole("button", { name: "Custom range" }).click();
  await toggleSidebar(page, "Expand sidebar");
  await page.mouse.move(600, 400);
  await page.mouse.wheel(0, 260);
  await expect(page.getByRole("button", { name: "Custom range" })).toBeInViewport({ ratio: 1 });
  await expectShown(rangeMenu(page));
});

test("at 720x360 and 125% text, with Findings scrolled, expanding the sidebar under an open Custom range keeps the dates menu and Done shown", async ({ page }) => {
  await openFolded(page, 720, 360);
  await page.getByRole("main").evaluate((node) => node.scrollBy(0, 95));
  await page.getByRole("button", { name: "Custom range" }).click();
  await toggleSidebar(page, "Expand sidebar");
  await expectShown(rangeMenu(page));
});

test("with Custom range open on a 650px window and the sidebar expanded, widening to 720px, scrolling Findings and folding the sidebar keeps the dates menu and Done shown", async ({ page }) => {
  await page.setViewportSize({ width: 650, height: 360 });
  await page.addInitScript(() => localStorage.setItem("holophyte.textSize", "125"));
  await page.goto("/#/findings?from=2026-09-30&to=2026-09-30");
  await expect(page.locator("[data-count-line]")).toBeVisible();
  await page.getByRole("button", { name: "Custom range" }).click();
  await page.setViewportSize({ width: 720, height: 360 });
  await page.getByRole("main").evaluate((node) => node.scrollBy(0, 15));
  await toggleSidebar(page, "Collapse sidebar");
  await expectShown(rangeMenu(page));
});

test("with classic scrollbars at 720x425 and 125% text, expanding the folded sidebar under an open Custom range keeps the dates menu clear of the scrollbar and Findings unscrolled sideways", async ({ playwright, baseURL }) => {
  const browser = await playwright.chromium.launch({ ignoreDefaultArgs: ["--hide-scrollbars"] });
  try {
    const page = await (await browser.newContext({ baseURL })).newPage();
    await openFolded(page, 720, 425);
    await page.getByRole("button", { name: "Custom range" }).click();
    await toggleSidebar(page, "Expand sidebar");
    await expectShown(rangeMenu(page));
    expect(await page.getByRole("main").evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  } finally {
    await browser.close();
  }
});
