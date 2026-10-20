import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

const out = process.env.CAPTURE_OUT ?? "";
const scratch = mkdtempSync(path.join(tmpdir(), "holo-442-"));
const SHOT = path.join(scratch, "console.png");

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), animations: "disabled" });
}

const lane = (page: Page) => page.getByRole("region", { name: "Pull requests", exact: true });
const row = (page: Page, ticket: string) => lane(page).locator("[data-pr-row]").filter({ hasText: ticket });

/** A real console screenshot and a short recording of it stand in for the
 *  pull request's media, served at the fixture's `media.example` URLs. */
async function makeMedia(browser: Browser, baseURL: string) {
  const context = await browser.newContext({ baseURL, viewport: { width: 1280, height: 800 }, recordVideo: { dir: scratch, size: { width: 640, height: 400 } } });
  const page = await context.newPage();
  await page.goto("/next.html#/tickets");
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: SHOT });
  await page.goto("/next.html#/");
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(3000);
  const video = page.video()!;
  await context.close();
  return video.path();
}

async function serveMedia(page: Page, clip: string) {
  await page.route(/^https:\/\/media\.example\/.*\.png$/, (route) => route.fulfill({ path: SHOT, contentType: "image/png" }));
  await page.route(/^https:\/\/media\.example\/.*\.webm$/, (route) => route.fulfill({ path: clip, contentType: "video/webm" }));
}

async function openDemo9(page: Page) {
  await page.goto("/next.html#/");
  const demo9 = row(page, "DEMO-9");
  await demo9.getByRole("button", { name: /^PR #42, / }).click();
  await expect(demo9.locator("[data-media-tile]")).toHaveCount(4);
  await expect(demo9.getByRole("button", { name: "Video: video-01.webm" })).toBeVisible();
  for (const image of await demo9.locator("[data-media-tile] img").all()) {
    await image.scrollIntoViewIfNeeded();
    await expect.poll(() => image.evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth > 0)).toBe(true);
  }
  return demo9;
}

async function openSecond(page: Page) {
  const demo9 = row(page, "DEMO-9");
  await demo9.getByRole("button", { name: "02 custom slot", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Screenshot 2 of 4" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("2 / 4")).toBeVisible();
  await expect.poll(() => dialog.locator("img").evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth > 0)).toBe(true);
  return dialog;
}

test("a pull request row's media tiles and their lightbox", async ({ page, browser, baseURL }, testInfo) => {
  test.setTimeout(120_000);
  const clip = await makeMedia(browser, baseURL!);
  await serveMedia(page, clip);
  const demo9 = await openDemo9(page);
  await demo9.locator("[data-media]").scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, 200));
  await shot(page, "01-pr-media-tiles.png");

  const dialog = await openSecond(page);
  await expect(dialog.getByRole("button", { name: "Previous" })).toBeEnabled();
  await expect(dialog.getByRole("button", { name: "Next" })).toBeEnabled();
  await shot(page, "02-pr-lightbox-image.png");

  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await openSecond(page);
  await shot(page, "03-pr-lightbox-image-night.png");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  await openSecond(page);
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  const last = page.getByRole("dialog", { name: "Video 4 of 4" });
  await expect(last.getByText("4 / 4")).toBeVisible();
  await expect(last.getByRole("button", { name: "Next" })).toBeDisabled();
  const video = last.locator("video");
  await expect.poll(() => video.evaluate((node: HTMLVideoElement) => node.readyState)).toBeGreaterThanOrEqual(1);
  await video.evaluate(async (node: HTMLVideoElement) => {
    await node.play();
    await new Promise<void>((done) => {
      const check = () => {
        if (node.currentTime < 2) return;
        node.removeEventListener("timeupdate", check);
        node.pause();
        done();
      };
      node.addEventListener("timeupdate", check);
    });
  });
  await video.hover();
  await page.waitForTimeout(400);
  await shot(page, "04-pr-lightbox-video.png");

  const focused = () => page.evaluate(() => {
    const active = document.activeElement;
    return active instanceof HTMLVideoElement ? "video" : (active?.getAttribute("aria-label") ?? active?.tagName ?? "");
  });
  await last.getByRole("button", { name: "Close" }).focus();
  const walk: string[] = [];
  for (let press = 0; press < 12; press += 1) {
    await page.keyboard.press("Tab");
    walk.push(await focused());
  }
  expect(walk.some((stop, index) => stop === "video" && walk[index + 1] === "video")).toBe(true);
  expect(walk).toContain("Previous");
  expect(walk).toContain("Close");
  expect(walk.every((stop) => ["video", "Previous", "Close"].includes(stop))).toBe(true);

  const recording = page.video();
  await page.close();
  if (recording) await recording.saveAs(testInfo.outputPath("pr-lightbox-flow.webm"));
});

test("the lightbox at 390 px fits the image to the width", async ({ browser, baseURL }) => {
  const clip = await makeMedia(browser, baseURL!);
  const context = await browser.newContext({ baseURL, viewport: { width: 390, height: 844 }, colorScheme: "light" });
  const page = await context.newPage();
  await serveMedia(page, clip);
  await openDemo9(page);
  const dialog = await openSecond(page);
  const image = await dialog.locator("img").boundingBox();
  expect(image!.x).toBeGreaterThanOrEqual(0);
  expect(image!.x + image!.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await shot(page, "05-pr-lightbox-390.png");
  await context.close();
});
