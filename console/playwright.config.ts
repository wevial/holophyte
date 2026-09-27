import { defineConfig } from "@playwright/test";

// The capture runner's config: tests.console_fixture serves the console from
// a seeded host daemon and names it in CONSOLE_URL, so there is no webServer.
export default defineConfig({
  testDir: "e2e",
  workers: 1,
  retries: 0,
  reporter: "line",
  use: {
    baseURL: process.env.CONSOLE_URL,
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
