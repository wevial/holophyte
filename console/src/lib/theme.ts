export type Theme = "system" | "light" | "dark";

export const THEME_KEY = "holophyte.theme";
export const THEMES: Theme[] = ["system", "light", "dark"];

/** The persisted choice under `key`, `fallback` when nothing valid is stored
 *  or storage is unavailable. */
function readChoice<T extends string>(key: string, choices: T[], fallback: T): T {
  try {
    const stored = localStorage.getItem(key);
    return choices.find((choice) => choice === stored) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeChoice(key: string, value: string, fallback: string): void {
  try {
    if (value === fallback) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // Storage may be disabled; the stamp still applies for this page.
  }
}

export function readTheme(): Theme {
  return readChoice(THEME_KEY, THEMES, "system");
}

export function writeTheme(theme: Theme): void {
  writeChoice(THEME_KEY, theme, "system");
}

/** Stamp `data-theme` on the document: absent for `system` so the
 *  `prefers-color-scheme` rule in theme.css decides. */
export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement): void {
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export type ThemeMode = "light" | "dark";
export type ThemePick = "lotus" | "dune" | "tide" | "mono" | "forest";

const THEME_PICKS: ThemePick[] = ["lotus", "dune", "tide", "mono", "forest"];
const PICK_KEYS: Record<ThemeMode, string> = { light: "holophyte.theme.light", dark: "holophyte.theme.dark" };

export function readPick(mode: ThemeMode): ThemePick {
  return readChoice(PICK_KEYS[mode], THEME_PICKS, "lotus");
}

export function writePick(mode: ThemeMode, pick: ThemePick): void {
  writeChoice(PICK_KEYS[mode], pick, "lotus");
}

/** Stamp `data-light` or `data-dark`: absent for `lotus`, whose values are
 *  theme.css's default blocks. */
export function applyPick(mode: ThemeMode, pick: ThemePick, root: HTMLElement = document.documentElement): void {
  const attribute = `data-${mode}`;
  if (pick === "lotus") root.removeAttribute(attribute);
  else root.setAttribute(attribute, pick);
}
