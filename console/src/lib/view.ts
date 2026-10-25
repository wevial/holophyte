/** Written only by the desktop app's seed script, on macOS. */
export const APP_SHELL_KEY = "holophyte.app-shell";

export const APP_DRAG = "[-webkit-app-region:drag]";
export const APP_NO_DRAG = "[-webkit-app-region:no-drag]";

/** Whether the page sits in the macOS app, under its hidden title bar;
 *  false in a browser or when storage is unavailable. */
export function inMacApp(): boolean {
  try {
    return localStorage.getItem(APP_SHELL_KEY) === "macos";
  } catch {
    return false;
  }
}

