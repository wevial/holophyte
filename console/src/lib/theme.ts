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
