/**
 * The `electron` module as the tray tests need it: just enough surface for
 * `main.ts` to load and for `trayImage` to run. `app.whenReady` never
 * resolves so the app boot stays inert; `app.getAppPath()` answers a temp
 * dir the test fills with the dist PNGs it wants found; and
 * `nativeImage.createFromPath` records the file it was asked to load so a
 * test can compare the picks instead of trusting a real NativeImage.
 * `nativeTheme` is the knob the old `-dark` pick read.
 */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

export const appDir = mkdtempSync(path.join(tmpdir(), "tray-app-"));
export const imagePaths: string[] = [];
export const nativeTheme = { shouldUseDarkColors: false };

export const app = {
  isPackaged: false,
  getAppPath: () => appDir,
  getPath: () => appDir,
  whenReady: () => new Promise(() => {}),
  on: () => {},
  getLoginItemSettings: () => ({ openAtLogin: false }),
  setLoginItemSettings: () => {},
  quit: () => {},
};
export const nativeImage = {
  createFromPath: (file: string) => {
    imagePaths.push(file);
    return { setTemplateImage: () => {} };
  },
};
export const Menu = { buildFromTemplate: (items: unknown) => items };
export class Tray {}
export class BrowserWindow {}
export const dialog = { showErrorBox: () => {} };
export const shell = { openExternal: () => Promise.resolve() };
