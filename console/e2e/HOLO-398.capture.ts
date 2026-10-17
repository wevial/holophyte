import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import type { ConfigBody } from "../src/lib/config";

const out = process.env.CAPTURE_OUT ?? "";
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(out, name), fullPage: true });
}

test("review, save, conflict and field refusal", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 },
    colorScheme: "light", timezoneId: "UTC", recordVideo: { dir: out, size: { width: 1440, height: 900 } } });
  const page = await context.newPage();
  const headers = { Authorization: `Bearer ${process.env.CONSOLE_TOKEN!}` };
  const statusResponse = await page.request.get("/projects/demo/status", { headers });
  const attentionResponse = await page.request.get("/projects/demo/attention", { headers });
  expect(statusResponse.ok()).toBe(true);
  expect(attentionResponse.ok()).toBe(true);
  const status = await statusResponse.json();
  const attention = await attentionResponse.json();
  await page.route("**/peers", route => route.fulfill({ json: { self: new URL(baseURL!).host, peers: [] } }));
  await page.route("**/status", route => route.fulfill({ json: { ...status, host: "writer-host", config_edit: true, actions: true } }));
  await page.route("**/attention", route => route.fulfill({ json: attention }));
  let config: ConfigBody = {
    path: "/srv/demo/config.toml", applies: "next loop start", revision: "3f9a2c1b0123456789abcdef",
    text: '[merge]\nmode = "pr"\napprove = "auto"\n[loop]\nworkers = 2\n',
    values: { merge: { mode: "pr", approve: "auto" }, loop: { workers: 2 } },
    saved: { at_ms: Date.parse("2026-10-14T09:12:00Z"), actor: "maintainer" },
    modified_ms: Date.parse("2026-10-14T09:12:01Z"),
  };
  let verdict: "saved" | "conflict" | "refused" = "saved";
  const refusal = "[holo2] /x/config.toml: [loop] workers must be an integer >= 1, got 0";
  await page.route("**/config", async route => {
    if (route.request().method() === "GET") return route.fulfill({ json: config });
    if (verdict === "conflict") {
      config = { ...config, revision: "8b2d9e0abcdef0123456789", modified_ms: Date.parse("2026-10-15T10:00:00Z") };
      return route.fulfill({ status: 409, json: { current: config.revision } });
    }
    if (verdict === "refused") return route.fulfill({ status: 400, json: { error: refusal } });
    expect(route.request().headers()["if-match"]).toBe(config.revision);
    expect(route.request().postDataJSON()).toEqual({ patch: { "merge.approve": "human", "loop.workers": 3 } });
    config = { ...config, revision: "7e4b6d2a0123456789abcdef", values: { merge: { mode: "pr", approve: "human" }, loop: { workers: 3 } },
      text: '[merge]\nmode = "pr"\napprove = "human"\n[loop]\nworkers = 3\n',
      saved: { at_ms: Date.parse("2026-10-14T09:15:00Z"), actor: "maintainer" }, modified_ms: Date.parse("2026-10-14T09:15:01Z") };
    return route.fulfill({ json: { revision: config.revision, backup: "config.toml.bak", applies: config.applies } });
  });
  await page.addInitScript(({ base, token }) => localStorage.setItem(`holophyte.token.${new URL(base).host}`, token),
    { base: baseURL!, token: process.env.CONSOLE_TOKEN! });
  await page.goto("/next.html#/settings/general");
  await page.getByRole("button", { name: "Human", exact: true }).click();
  await page.getByRole("button", { name: "One more worker" }).click();
  await page.getByRole("button", { name: "Review and save" }).click();
  const dialog = page.getByRole("dialog", { name: "Save these changes?" });
  await expect(dialog).toContainText("Applies at the next loop start.");
  await expect(dialog).toContainText("workers = 3");
  await shot(page, "01-save-confirm-dawn.png");
  await dialog.getByRole("button", { name: "Keep editing" }).click();
  await page.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).click();
  await page.getByRole("button", { name: "Review and save" }).click();
  await expect(dialog).toBeVisible();
  await shot(page, "02-save-confirm-night.png");
  await dialog.getByRole("button", { name: "Keep editing" }).click();
  await page.getByRole("button", { name: "Colour theme: night. Switch to dawn" }).click();
  await page.getByRole("button", { name: "Review and save" }).click();
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved config.toml" })).toContainText("7e4b6d2");
  await expect(page.getByRole("button", { name: "Restart supervisor", exact: true })).toBeVisible();
  await expect(page.getByText(/demo · config.toml on writer-host · revision 7e4b6d2/)).toBeVisible();
  await shot(page, "03-save-banner.png");
  await page.getByRole("button", { name: "Later", exact: true }).click();
  verdict = "conflict";
  await page.getByRole("button", { name: "One more worker" }).click();
  await page.getByRole("button", { name: "Review and save" }).click();
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toContainText("config.toml changed on writer-host since this page read it. It has been read again: check the fields and save once more.");
  await shot(page, "04-save-conflict.png");
  verdict = "refused";
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("group", { name: "Workers", exact: true }).getByRole("alert")).toHaveText(refusal);
  await shot(page, "05-save-refused.png");
  const video = page.video();
  await context.close();
  if (video) {
    await video.saveAs(path.join(out, "settings-save-flow.webm"));
    await video.delete();
  }
});
