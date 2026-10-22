/**
 * The console window's options, pure so a test can read them: `main.ts`
 * hands `windowOptions(process.platform)` to `new BrowserWindow`. On macOS
 * the title bar is hidden and the traffic lights sit in the sidebar
 * header; the background is the dark `--canvas` from
 * `console/src/next/theme.css`, so no light frame shows before the paint.
 */
import type { BrowserWindowConstructorOptions } from "electron";

export function windowOptions(platform: NodeJS.Platform): BrowserWindowConstructorOptions {
  const options: BrowserWindowConstructorOptions = {
    width: 1280,
    height: 860,
    minWidth: 1100,
    minHeight: 600,
    title: "Holophyte Console",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  };
  if (platform !== "darwin") return options;
  return {
    ...options,
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 14, y: 23 },
    backgroundColor: "#05070f",
  };
}
