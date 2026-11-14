export type Theme = "system" | "light" | "dark";

export const THEME_KEY = "holophyte.theme";
export const THEMES: Theme[] = ["system", "light", "dark"];

function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (THEMES as string[]).includes(value);
}

/** The persisted choice, `system` when nothing valid is stored or storage
 *  is unavailable. */
export function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return isTheme(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function writeTheme(theme: Theme): void {
  try {
    if (theme === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Storage may be disabled; the stamp still applies for this page.
  }
}

/** Stamp `data-theme` on the document: absent for `system` so the
 *  `prefers-color-scheme` rule in theme.css decides. */
export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement): void {
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export type ThemeMode = "light" | "dark";
export type ThemePick = "lotus" | "dune" | "tide" | "mono" | "forest";

export const THEME_PICKS: ThemePick[] = ["lotus", "dune", "tide", "mono", "forest"];
export const PICK_KEYS: Record<ThemeMode, string> = { light: "holophyte.theme.light", dark: "holophyte.theme.dark" };

function isPick(value: unknown): value is ThemePick {
  return typeof value === "string" && (THEME_PICKS as string[]).includes(value);
}

/** The persisted theme for one mode, `lotus` when nothing valid is stored
 *  or storage is unavailable. */
export function readPick(mode: ThemeMode): ThemePick {
  try {
    const stored = localStorage.getItem(PICK_KEYS[mode]);
    return isPick(stored) ? stored : "lotus";
  } catch {
    return "lotus";
  }
}

export function writePick(mode: ThemeMode, pick: ThemePick): void {
  try {
    if (pick === "lotus") localStorage.removeItem(PICK_KEYS[mode]);
    else localStorage.setItem(PICK_KEYS[mode], pick);
  } catch {
    // Storage may be disabled; the stamp still applies for this page.
  }
}

/** Stamp `data-light` or `data-dark`: absent for `lotus`, whose values are
 *  theme.css's default blocks. */
export function applyPick(mode: ThemeMode, pick: ThemePick, root: HTMLElement = document.documentElement): void {
  const attribute = `data-${mode}`;
  if (pick === "lotus") root.removeAttribute(attribute);
  else root.setAttribute(attribute, pick);
}
