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
  await expect(page.locator("[data-page-count]")).toBeVisible();
}

async function toggleSidebar(page: Page, name: "Expand sidebar" | "Collapse sidebar") {
  await expect(page.getByRole("button", { name })).toBeVisible();
  await page.keyboard.press("Control+Backslash");
  await expect(page.getByRole("button", { name })).toHaveCount(0);
}

const rangeMenu = (page: Page) => page.getByRole("group", { name: "Range range" });

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
  await expect(page.locator("[data-page-count]")).toBeVisible();
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

const SECTIONS = ["Severity", "Category", "Status"];

async function openFilter(page: Page, width: number, height: number, textSize: number, folded: boolean) {
  await page.setViewportSize({ width, height });
  await page.addInitScript(([size, rail]) => {
    localStorage.setItem("holophyte.textSize", size!);
    if (rail != null) localStorage.setItem("holophyte.next.rail", rail);
  }, [String(textSize), folded ? "folded" : null]);
  await page.goto("/#/findings");
  await expect(page.locator("[data-page-count]")).toBeVisible();
  await expect(page.getByRole("button", { name: folded ? "Expand sidebar" : "Collapse sidebar" })).toBeVisible();
  await page.getByRole("button", { name: /^Filter findings/ }).click();
  return page.getByRole("dialog", { name: "Filter findings" });
}

/** The dialog's box and every option and heading label in its sections, each against the part of the scroll pane the reader sees, any label
 *  cut short, any label whose ends are not hit-tested to it, as one scrolled out of the dialog is not, and the dialog over its own button. */
async function cutOff(dialog: Locator) {
  return dialog.evaluate((node, sections) => {
    let pane: HTMLElement = node.parentElement!;
    while (pane !== document.documentElement && !/auto|scroll/.test(getComputedStyle(pane).overflowX)) pane = pane.parentElement!;
    const box = pane.getBoundingClientRect();
    const scale = box.width / (pane.offsetWidth || 1);
    const shown = pane === document.documentElement
      ? { left: 0, top: 0, right: document.documentElement.clientWidth * scale, bottom: window.innerHeight }
      : {
          left: box.left + pane.clientLeft * scale, top: box.top + pane.clientTop * scale,
          right: box.left + (pane.clientLeft + pane.clientWidth) * scale, bottom: box.top + (pane.clientTop + pane.clientHeight) * scale,
        };
    const outside = (part: Element) => {
      const rect = part.getBoundingClientRect();
      return rect.left < shown.left - 0.5 || rect.right > shown.right + 0.5 || rect.top < shown.top - 0.5 || rect.bottom > shown.bottom + 0.5;
    };
    const hit = (part: Element) => {
      const rect = part.getBoundingClientRect();
      const y = rect.top + rect.height / 2;
      return [rect.left + 1, rect.right - 1].every((x) => part.contains(document.elementFromPoint(x, y)));
    };
    const cut: string[] = outside(node) ? ["dialog"] : [];
    const trigger = node.parentElement!.querySelector("button[aria-haspopup]")!.getBoundingClientRect();
    const sheet = node.getBoundingClientRect();
    if (sheet.top < trigger.bottom && sheet.bottom > trigger.top && sheet.left < trigger.right && sheet.right > trigger.left) cut.push("button");
    for (const name of sections) {
      const group = [...node.querySelectorAll("fieldset")].find((each) => each.querySelector("legend")?.textContent?.startsWith(name))!;
      for (const label of group.querySelectorAll("label")) {
        const text = label.querySelector("[data-option-label]") ?? label;
        if (outside(label) || !hit(text) || text.scrollWidth > text.clientWidth) cut.push(label.querySelector("input")!.getAttribute("aria-label")!);
      }
    }
    return cut;
  }, SECTIONS);
}

for (const [width, textSize] of [[720, 120], [740, 125]] as const) {
  for (const height of [900, 600, 500]) {
    for (const folded of [true, false]) {
      test(`at ${width}x${height} and ${textSize}% text with the sidebar ${folded ? "folded" : "expanded"}, Filter findings opens inside the scroll pane with every label shown whole`, async ({ page }) => {
        const dialog = await openFilter(page, width, height, textSize, folded);
        await expect(dialog.getByRole("checkbox", { name: "must" })).toBeAttached();
        await expect(async () => expect(await cutOff(dialog)).toEqual([])).toPass({ timeout: 2000 });
      });
    }
  }
}

test("at 873x900 and 125% text with the sidebar expanded, picking Uncategorized in Filter findings keeps every label shown whole", async ({ page }) => {
  const dialog = await openFilter(page, 873, 900, 125, false);
  await dialog.getByText("Uncategorized", { exact: true }).click();
  await expect(dialog.getByRole("checkbox", { name: "Uncategorized" })).toBeChecked();
  await expect(async () => expect(await cutOff(dialog)).toEqual([])).toPass({ timeout: 2000 });
});

test("at 760x900 and 125% text with the sidebar expanded, an open Filter findings holds one width frame after frame", async ({ page }) => {
  const dialog = await openFilter(page, 760, 900, 125, false);
  await expect(dialog.getByRole("checkbox", { name: "must" })).toBeAttached();
  const widths = await dialog.evaluate((node) => new Promise<number[]>((resolve) => {
    const seen: number[] = [];
    const sample = () => {
      seen.push(node.getBoundingClientRect().width);
      if (seen.length < 30) requestAnimationFrame(sample);
      else resolve(seen);
    };
    requestAnimationFrame(sample);
  }));
  expect(new Set(widths).size).toBe(1);
});

test("at 740x600 and 125% text with the sidebar expanded, picking unclassified at the foot of a scrolled Runs filter keeps it scrolled there", async ({ page }) => {
  await page.setViewportSize({ width: 740, height: 600 });
  await page.addInitScript(() => localStorage.setItem("holophyte.textSize", "125"));
  await page.goto("/#/runs");
  await expect(page.locator("li[data-run]").first()).toBeVisible();
  await page.getByRole("button", { name: /^Filter and sort/ }).click();
  const dialog = page.getByRole("dialog", { name: "Filter and sort" });
  await expect(dialog.getByRole("checkbox", { name: "unclassified" })).toBeAttached();
  const scrolled = await dialog.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
    return node.scrollTop;
  });
  expect(scrolled).toBeGreaterThan(0);
  await dialog.getByText("unclassified", { exact: true }).click();
  await expect(dialog.getByRole("checkbox", { name: "unclassified" })).toBeChecked();
  expect(await dialog.evaluate((node) => node.scrollTop)).toBe(scrolled);
  await expect(dialog.getByText("unclassified", { exact: true })).toBeInViewport({ ratio: 1 });
});

test("at 320px, Filter findings stacks Severity, Category and Status in one column with no label cut off", async ({ page }) => {
  const dialog = await openFilter(page, 320, 800, 100, false);
  await expect(async () => {
    const boxes = await Promise.all(SECTIONS.map(async (name) => (await dialog.getByRole("group", { name }).boundingBox())!));
    expect(boxes.map((box) => Math.round(box.x))).toEqual(boxes.map(() => Math.round(boxes[0]!.x)));
    for (let at = 1; at < boxes.length; at += 1) expect(boxes[at]!.y).toBeGreaterThanOrEqual(boxes[at - 1]!.y + boxes[at - 1]!.height);
    expect(await cutOff(dialog)).toEqual([]);
  }).toPass({ timeout: 2000 });
});
