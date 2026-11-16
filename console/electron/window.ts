/**
 * The console window's options, pure so a test can read them: `main.ts`
 * hands `windowOptions(process.platform)` to `new BrowserWindow`. On macOS
 * the title bar is hidden and the traffic lights sit in the console's own
 * 30px title bar; the background is the dark `--canvas` from
 * `console/src/app/theme.css`, so no light frame shows before the paint.
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
    trafficLightPosition: { x: 12, y: 8 },
    backgroundColor: "#0d0a15",
  };
}
