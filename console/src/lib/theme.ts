import { contrast, hexToOklch, oklchToHex, oklchToRgb, type Oklch } from "./oklch";

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
export type BuiltinPick = "lotus" | "dune" | "tide" | "mono" | "forest";
export type ThemePick = BuiltinPick | `custom:${string}`;

export const BUILTIN_PICKS: BuiltinPick[] = ["lotus", "dune", "tide", "mono", "forest"];
const PICK_KEYS: Record<ThemeMode, string> = { light: "holophyte.theme.light", dark: "holophyte.theme.dark" };

export function readPick(mode: ThemeMode): ThemePick {
  const customs = parseCustomThemes(readCustomThemesRaw()).map((theme) => theme.id);
  return readChoice<ThemePick>(PICK_KEYS[mode], [...BUILTIN_PICKS, ...customs], "lotus");
}

export function writePick(mode: ThemeMode, pick: ThemePick): void {
  writeChoice(PICK_KEYS[mode], pick, "lotus");
}

/** Stamp `data-light` or `data-dark`: absent for `lotus`, whose values are
 *  theme.css's default blocks. */
export function applyPick(mode: ThemeMode, pick: ThemePick, root: HTMLElement = document.documentElement): void {
  const attribute = `data-${mode}`;
  if (pick === "lotus") root.removeAttribute(attribute);
  else root.setAttribute(attribute, isCustom(pick) ? "custom" : pick);
}

export const isCustom = (pick: ThemePick): pick is `custom:${string}` => pick.startsWith("custom:");

export type HazeSource = "accent" | "hand";

export type ThemeSpec = {
  name: string;
  ground: Oklch;
  accent: Oklch;
  haze: { source: HazeSource; back: number; front: number };
  grain: number;
};

/** `origin` is the accent of the theme this one was copied from. */
export type CustomTheme = ThemeSpec & { id: `custom:${string}`; origin: Oklch };

export type ColourToken =
  | "canvas" | "side" | "raised" | "hover" | "selected" | "ink" | "ink-2" | "ink-3"
  | "accent" | "focus" | "live" | "ask" | "wait" | "bad" | "need";

export type DerivedTheme = {
  mode: ThemeMode;
  colours: Record<ColourToken, Oklch>;
  haze: { back: Oklch; front: Oklch };
  tokens: Record<string, string>;
};

const STATES: Record<ThemeMode, Record<"live" | "ask" | "wait" | "bad" | "need", [number, number, number]>> = {
  dark: { live: [0.82, 0.12, 155], ask: [0.86, 0.13, 90], wait: [0.78, 0.11, 285], bad: [0.74, 0.15, 25], need: [0.81, 0.09, 350] },
  light: { live: [0.52, 0.13, 155], ask: [0.53, 0.11, 80], wait: [0.48, 0.16, 285], bad: [0.53, 0.19, 25], need: [0.50, 0.16, 350] },
};
const INKS: Record<ThemeMode, [number, number][]> = {
  dark: [[0.94, 0.02], [0.80, 0.03], [0.66, 0.035]],
  light: [[0.22, 0.02], [0.42, 0.025], [0.52, 0.03]],
};
const HAZE_BANDS: Record<ThemeMode, { back: [number, number]; front: [number, number]; strength: string; alpha: [number, number] }> = {
  dark: { back: [0.45, 0.18], front: [0.70, 0.16], strength: "0.30", alpha: [0.34, 0.2] },
  light: { back: [0.84, 0.07], front: [0.85, 0.07], strength: "0.62", alpha: [0.7, 0.55] },
};

const hue = (h: number) => ((h % 360) + 360) % 360;
const at = (l: number, c: number, h: number): Oklch => ({ l, c, h: hue(h) });

function grainImage(opacity: number): string {
  return `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix values='1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 0 0 0 0 1'/%3E%3C/filter%3E%3Crect width='200' height='200' filter='url(%23n)' opacity='${opacity}'/%3E%3C/svg%3E")`;
}

function rgba(colour: Oklch, alpha: number): string {
  return `rgba(${oklchToRgb(colour).map((x) => Math.round(x * 255)).join(", ")}, ${alpha})`;
}

/** Every colour a custom theme sets, from its ground and accent; a ground under L 0.5 is dark. */
export function deriveTheme(spec: ThemeSpec): DerivedTheme {
  const { l, c, h } = spec.ground;
  const mode: ThemeMode = l < 0.5 ? "dark" : "light";
  const dark = mode === "dark";
  const accent = at(spec.accent.l, spec.accent.c, spec.accent.h);
  const [ink, ink2, ink3] = INKS[mode].map(([il, ic]) => at(il, ic, h)) as [Oklch, Oklch, Oklch];
  const states = Object.fromEntries(Object.entries(STATES[mode]).map(([name, [sl, sc, sh]]) => [name, at(sl, sc, sh)]));
  const colours = {
    canvas: at(l, c, h),
    side: at(dark ? l + 0.025 : l - 0.016, Math.min(0.09, 1.2 * c + 0.004), h),
    raised: dark ? at(l + 0.04, Math.min(0.09, 1.2 * c + 0.004), h) : at(1, 0, h),
    hover: dark ? at(l + 0.075, Math.min(0.09, 1.6 * c + 0.006), h) : at(l - 0.055, Math.min(0.04, 1.5 * c + 0.012), h),
    selected: dark ? at(l + 0.16, Math.min(0.08, 0.4 * accent.c), accent.h) : at(l - 0.14, Math.min(0.06, 0.3 * accent.c), accent.h),
    ink, "ink-2": ink2, "ink-3": ink3,
    accent, focus: accent,
    ...states,
  } as Record<ColourToken, Oklch>;
  const [backHue, frontHue] = spec.haze.source === "accent" ? [accent.h, accent.h + 45] : [spec.haze.back, spec.haze.front];
  const bands = HAZE_BANDS[mode];
  const haze = { back: at(...bands.back, backHue), front: at(...bands.front, frontHue) };
  const tokens: Record<string, string> = Object.fromEntries(Object.entries(colours).map(([name, colour]) => [`--${name}`, oklchToHex(colour)]));
  Object.assign(tokens, {
    "--haze-ground": tokens["--canvas"],
    "--haze-back": oklchToHex(haze.back),
    "--haze-front": oklchToHex(haze.front),
    "--haze-strength": bands.strength,
    "--haze-fallback-back": rgba(haze.back, bands.alpha[0]),
    "--haze-fallback-front": rgba(haze.front, bands.alpha[1]),
    "--grain": grainImage(spec.grain / 100),
    "--grain-side": grainImage(spec.grain / 200),
  });
  return { mode, colours, haze, tokens };
}

/** The text colour a primary button sets on the accent: the ground in a dark theme, `raised` in a light one. */
export function buttonInk(theme: DerivedTheme): string {
  return theme.tokens[theme.mode === "dark" ? "--canvas" : "--raised"]!;
}

export const BUILTIN_NAMES: Record<BuiltinPick, string> = { lotus: "Lotus", dune: "Dune", tide: "Tide", mono: "Mono", forest: "Forest" };

const BUILTIN_COLOURS: Record<ThemeMode, Record<BuiltinPick, [ground: string, accent: string, back: string, front: string]>> = {
  light: {
    lotus: ["#f6f5fa", "#5a4ebf", "#c8bff6", "#f3bddb"],
    dune: ["#fcfaf7", "#a4512e", "#f6c4ad", "#f0b5c8"],
    tide: ["#f5f7fb", "#3651c9", "#bcd0f5", "#cfc4f6"],
    mono: ["#fafafa", "#5a4ebf", "#d4ccf3", "#f2c3dc"],
    forest: ["#f6f8f4", "#1f7a5c", "#93cdb6", "#e8f0a2"],
  },
  dark: {
    lotus: ["#0d0a15", "#b9a3ff", "#4b3fc4", "#c77ee8"],
    dune: ["#1b1817", "#ffad7a", "#8a2f4f", "#e8785a"],
    tide: ["#0b0e1d", "#9db1ff", "#2c3cb0", "#8a6bff"],
    mono: ["#0a0a0b", "#b9a3ff", "#4b3fc4", "#d07ee8"],
    forest: ["#0e1411", "#7fd6b8", "#16594a", "#b4d368"],
  },
};

const round = (x: number) => Math.round(x * 1000) / 1000;
const rounded = (hex: string): Oklch => {
  const { l, c, h } = hexToOklch(hex);
  return { l: round(l), c: round(c), h: Math.round(h) % 360 };
};

/** A built-in theme as an editor spec: its ground, accent and haze hues read from theme.css's values. */
export function builtinSpec(mode: ThemeMode, pick: BuiltinPick): ThemeSpec {
  const [ground, accent, back, front] = BUILTIN_COLOURS[mode][pick];
  return {
    name: `${BUILTIN_NAMES[pick]} ${mode}`,
    ground: rounded(ground),
    accent: rounded(accent),
    haze: { source: "hand", back: rounded(back).h, front: rounded(front).h },
    grain: mode === "dark" ? 4 : 3,
  };
}

const CUSTOM_KEY = "holophyte.themes.custom";
const CUSTOM_PROPERTIES = [
  "--canvas", "--side", "--raised", "--hover", "--selected", "--ink", "--ink-2", "--ink-3", "--accent", "--focus",
  "--live", "--ask", "--wait", "--bad", "--need", "--haze-ground", "--haze-back", "--haze-front", "--haze-strength",
  "--haze-fallback-back", "--haze-fallback-front", "--grain", "--grain-side",
];

const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const isOklch = (value: unknown): value is Oklch =>
  typeof value === "object" && value !== null && [value as Oklch].every(({ l, c, h }) => isNumber(l) && isNumber(c) && isNumber(h));

function isCustomTheme(value: unknown): value is CustomTheme {
  if (typeof value !== "object" || value === null) return false;
  const theme = value as CustomTheme;
  return typeof theme.id === "string" && theme.id.startsWith("custom:") && typeof theme.name === "string"
    && isOklch(theme.ground) && isOklch(theme.accent) && isOklch(theme.origin) && isNumber(theme.grain)
    && typeof theme.haze === "object" && theme.haze !== null && ["accent", "hand"].includes(theme.haze.source)
    && isNumber(theme.haze.back) && isNumber(theme.haze.front);
}

export function readCustomThemesRaw(): string | null {
  try {
    return localStorage.getItem(CUSTOM_KEY);
  } catch {
    return null;
  }
}

export function parseCustomThemes(raw: string | null): CustomTheme[] {
  try {
    const parsed: unknown = JSON.parse(raw ?? "[]");
    return Array.isArray(parsed) ? parsed.filter(isCustomTheme) : [];
  } catch {
    return [];
  }
}

export function writeCustomThemes(themes: CustomTheme[]): void {
  writeChoice(CUSTOM_KEY, JSON.stringify(themes), "[]");
}

export function applyCustomTheme(theme: CustomTheme | null, root: HTMLElement = document.documentElement): void {
  for (const property of CUSTOM_PROPERTIES) root.style.removeProperty(property);
  if (!theme) return;
  for (const [property, value] of Object.entries(deriveTheme(theme).tokens)) root.style.setProperty(property, value);
}

/** Text pairs need 4.5:1 and state colours 3:1, each against the ground unless named otherwise. */
export function contrastChecks(theme: DerivedTheme) {
  const t = theme.tokens;
  const check = (name: string, colour: string, on: string, bar: number) => ({ name, colour, ratio: contrast(colour, on), bar });
  return [
    check("Text", t["--ink"]!, t["--canvas"]!, 4.5),
    check("Secondary text", t["--ink-2"]!, t["--canvas"]!, 4.5),
    check("Muted text", t["--ink-3"]!, t["--canvas"]!, 4.5),
    check("Sidebar muted text", t["--ink-3"]!, t["--side"]!, 4.5),
    check("Accent", t["--accent"]!, t["--canvas"]!, 4.5),
    check("Button text", buttonInk(theme), t["--accent"]!, 4.5),
    check("Running", t["--live"]!, t["--canvas"]!, 3),
    check("Asking", t["--ask"]!, t["--canvas"]!, 3),
    check("Waiting", t["--wait"]!, t["--canvas"]!, 3),
    check("Failed", t["--bad"]!, t["--canvas"]!, 3),
    check("Needs you", t["--need"]!, t["--canvas"]!, 3),
  ];
}
