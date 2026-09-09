/**
 * The desktop shell around the console the daemon serves. This is the only
 * file that imports `electron`; it owns the window, the tray and the app
 * lifecycle and nothing else — no renderer code, no preload, no IPC.
 */
import { readFileSync } from "node:fs";
import path from "node:path";

import { BrowserWindow, Menu, Tray, app, dialog, nativeImage } from "electron";

import { CONFIG_FILE, resolveConsoleUrl } from "./config.ts";
import { menuTemplate } from "./menu.ts";

// The SwiftBar drawer's template icon; nativeImage picks up the @2x sibling
// by name. From the repository, app.getAppPath() is this package's directory
// (the one holding package.json) and the icon sits in the repo's assets/.
// Packaged, electron-builder copies both PNGs (extraResources in
// electron-builder.yml) into Contents/Resources/assets/.
function trayIconPath(): string {
  const assetsDir = app.isPackaged
    ? path.join(process.resourcesPath, "assets")
    : path.resolve(app.getAppPath(), "..", "..", "assets");
  return path.join(assetsDir, "menubar-template@1x.png");
}

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;

function readConfigFile(): string | null {
  try {
    return readFileSync(path.join(app.getPath("userData"), CONFIG_FILE), "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  }
}

function showConsole(url: string): void {
  if (mainWindow !== null) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return;
  }
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 1100,
    minHeight: 600,
    title: "Holophyte Console",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  void mainWindow.loadURL(url);
}

function addTray(url: string): void {
  const icon = nativeImage.createFromPath(trayIconPath());
  if (process.platform === "darwin") icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Holophyte Console");
  tray.setContextMenu(
    Menu.buildFromTemplate(
      menuTemplate(
        { openAtLogin: app.getLoginItemSettings().openAtLogin },
        {
          showConsole: () => showConsole(url),
          // macOS keeps the login item itself, so the state survives a restart.
          setOpenAtLogin: (enabled) => app.setLoginItemSettings({ openAtLogin: enabled }),
          quit: () => app.quit(),
        },
      ),
    ),
  );
}

app.whenReady().then(() => {
  const resolved = resolveConsoleUrl(process.env, readConfigFile());
  if ("error" in resolved) {
    dialog.showErrorBox("Holophyte Console", `Cannot open the console.\n\n${resolved.error}`);
    app.quit();
    return;
  }
  const { url } = resolved;
  addTray(url);
  showConsole(url);
  app.on("activate", () => showConsole(url));
});

app.on("window-all-closed", () => {
  // On macOS the tray keeps the app alive; elsewhere closing the window quits.
  if (process.platform !== "darwin") app.quit();
});
