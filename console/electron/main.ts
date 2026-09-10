/**
 * The desktop shell around the console the daemon serves. This is the only
 * file that imports `electron`; it owns the window, the tray and the app
 * lifecycle and nothing else — no renderer code, no preload, no IPC.
 */
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { BrowserWindow, Menu, Tray, app, dialog, nativeImage } from "electron";

import { CONFIG_FILE, resolveConsoleUrl } from "./config.ts";
import { appendLog, consoleLine, failedLoadLine } from "./log.ts";
import type { MenuActions } from "./menu.ts";
import { POLL_INTERVAL_MS, pollAll, readTokens } from "./poll.ts";
import { seedScript } from "./seed.ts";
import { type Level, buildSummary, summarizeAnswer } from "./tray.ts";

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

// The drawer's warn and bad variants (assets/menubar-{warn,bad}.svg) carry
// the state dot inside the glyph. Electron cannot load an SVG into a tray,
// so `bun run icon` renders them to PNG in dist/ (`files` in
// electron-builder.yml carries them into the bundle); a missing render
// falls back to the template glyph, as the drawer does.
const VARIANT: Partial<Record<Level, string>> = { attention: "warn", bad: "bad" };

function trayImage(level: Level): Electron.NativeImage {
  const variant = VARIANT[level];
  if (variant !== undefined) {
    const file = path.join(app.getAppPath(), "dist", `menubar-${variant}@1x.png`);
    if (existsSync(file)) return nativeImage.createFromPath(file);
  }
  const icon = nativeImage.createFromPath(trayIconPath());
  if (process.platform === "darwin") icon.setTemplateImage(true);
  return icon;
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

function showConsole(url: string, configText: string | null): void {
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
  // Seed the page's storage with the bearers console.json holds, under the
  // keys console/src/lib/token.ts reads, so a host the tray can see needs
  // no pasting. When the script changed a key the window reloads once so
  // the first poll carries it; the load after that changes nothing.
  const contents = mainWindow.webContents;
  contents.on("did-finish-load", () => {
    const tokens = readTokens(configText, app.getPath("userData"));
    contents
      .executeJavaScript(seedScript(tokens))
      .then((changed: unknown) => {
        if (changed === true) contents.reload();
      })
      .catch((err: unknown) => console.error("token seed failed:", err));
  });
  // What the page saw when a view went blank in the app but not in a
  // browser: every console error and failed load, one line each, in
  // console.log beside console.json (log.ts owns the truncation and keeps
  // query strings — where a token could travel — out of the lines).
  const log = (line: string): void => {
    try {
      appendLog(app.getPath("userData"), line);
    } catch (err) {
      console.error("console.log append failed:", err);
    }
  };
  contents.on("console-message", (event) => {
    if (event.level === "error") log(consoleLine(event.message, event.sourceId, event.lineNumber));
  });
  contents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    log(failedLoadLine(errorCode, errorDescription, validatedURL));
  });
  void mainWindow.loadURL(url);
}

function toggleDevTools(url: string, configText: string | null): void {
  showConsole(url, configText);
  mainWindow?.webContents.toggleDevTools();
}

function trayActions(url: string, configText: string | null): MenuActions {
  return {
    showConsole: () => showConsole(url, configText),
    // macOS keeps the login item itself, so the state survives a restart.
    setOpenAtLogin: (enabled) => app.setLoginItemSettings({ openAtLogin: enabled }),
    toggleDevTools: () => toggleDevTools(url, configText),
    quit: () => app.quit(),
  };
}

// The tray carries the drawer's summary: every POLL_INTERVAL_MS, /peers on
// the console URL, then /status and /attention (and /runs when idle) on each
// daemon it names, with
// the bearer console.json holds per address. A poll that throws (it should
// not: every fetch failure is a result) leaves the last menu in place.
async function refreshTray(url: string, configText: string | null): Promise<void> {
  if (tray === null) return;
  const tokens = readTokens(configText, app.getPath("userData"));
  const answer = await pollAll(url, tokens);
  if (tray === null) return;
  const { items, level } = summarizeAnswer(answer, Date.now(), {
    state: { openAtLogin: app.getLoginItemSettings().openAtLogin },
    actions: trayActions(url, configText),
  });
  tray.setContextMenu(Menu.buildFromTemplate(items));
  tray.setImage(trayImage(level));
}

function addTray(url: string, configText: string | null): void {
  tray = new Tray(trayImage("idle"));
  tray.setToolTip("Holophyte Console");
  const { items } = buildSummary([], {}, {}, Date.now(), {
    state: { openAtLogin: app.getLoginItemSettings().openAtLogin },
    actions: trayActions(url, configText),
  });
  tray.setContextMenu(Menu.buildFromTemplate(items));
  const tick = (): void => {
    refreshTray(url, configText).catch((err: unknown) => console.error("tray poll failed:", err));
  };
  tick();
  setInterval(tick, POLL_INTERVAL_MS);
}

app.whenReady().then(() => {
  const configText = readConfigFile();
  const resolved = resolveConsoleUrl(process.env, configText);
  if ("error" in resolved) {
    dialog.showErrorBox("Holophyte Console", `Cannot open the console.\n\n${resolved.error}`);
    app.quit();
    return;
  }
  const { url } = resolved;
  addTray(url, configText);
  showConsole(url, configText);
  app.on("activate", () => showConsole(url, configText));
});

app.on("window-all-closed", () => {
  // On macOS the tray keeps the app alive; elsewhere closing the window quits.
  if (process.platform !== "darwin") app.quit();
});
