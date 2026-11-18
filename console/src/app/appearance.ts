import { useSyncExternalStore } from "react";
import {
  applyCustomTheme, applyPick, applyTheme, deriveTheme, isCustom, parseCustomThemes, readCustomThemesRaw, readPick, readTheme, writeCustomThemes, writePick, writeTheme,
  type CustomTheme, type Theme, type ThemeMode, type ThemePick,
} from "../lib/theme";

export type ReadingFont = "sans" | "serif" | "hyperlegible";

export type Density = "comfortable" | "compact";

const DENSITY_KEY = "holophyte.next.density";
const FONT_KEY = "holophyte.next.font";
const DARK_QUERY = "(prefers-color-scheme: dark)";
const EFFECT_KEY = "holophyte.next.effect";
const MOTION_KEY = "holophyte.next.motion";

const TEXT_SIZE_KEY = "holophyte.textSize";
const GRAIN_KEY = "holophyte.grain";
const HAZE_STRENGTH_KEY = "holophyte.hazeStrength";
const EFFECTS_KEY = "holophyte.effects";
const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

const HEADER_EFFECTS = ["haze", "caustics", "plume", "ribbons", "lava", "hyphae", "none"] as const;

export type HeaderEffect = (typeof HEADER_EFFECTS)[number];

export type BandEffect = Exclude<HeaderEffect, "none">;

type EffectSettings = Partial<Record<BandEffect, { fade: number }>>;

export const TEXT_SIZES = { min: 90, max: 125, step: 5 };
export const STRENGTHS = { min: 0, max: 200, step: 5 };
export const FULL = 100;
export const FADES = { min: 0, max: 100, step: 5 };
export const DEFAULT_FADE = 70;

function readHeaderEffect(fallback: HeaderEffect = "haze"): HeaderEffect {
  try {
    const stored = localStorage.getItem(EFFECT_KEY);
    return HEADER_EFFECTS.find((effect) => effect === stored) ?? "haze";
  } catch {
    return fallback;
  }
}

function writeStored(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    return;
  }
}

const inRange = (value: unknown, range: typeof STRENGTHS): value is number =>
  Number.isInteger(value) && (value as number) >= range.min && (value as number) <= range.max && (value as number) % range.step === 0;

function readPercent(key: string, range: typeof STRENGTHS) {
  return (fallback = FULL): number => {
    try {
      const stored = Number(localStorage.getItem(key) ?? FULL);
      return inRange(stored, range) ? stored : FULL;
    } catch {
      return fallback;
    }
  };
}

function writePercent(key: string) {
  return (value: number) => writeStored(key, value === FULL ? null : String(value));
}

function applyScale(property: string) {
  return (percent: number) => {
    if (percent === FULL) document.documentElement.style.removeProperty(property);
    else document.documentElement.style.setProperty(property, String(percent / 100));
  };
}

/** Zoom the document, not the shell, so dialogs and toasts portalled to the body scale too. */
export function applyTextSize(percent: number, root: HTMLElement = document.documentElement): void {
  const scaled = percent !== FULL;
  for (const property of ["zoom", "--text-scale"]) {
    if (scaled) root.style.setProperty(property, String(percent / 100));
    else root.style.removeProperty(property);
  }
  root.toggleAttribute("data-text-size", scaled);
  root.toggleAttribute("data-text-floor", percent < FULL);
}

export const readGrain = readPercent(GRAIN_KEY, STRENGTHS);
export const readHazeStrength = readPercent(HAZE_STRENGTH_KEY, STRENGTHS);
export const applyGrain = applyScale("--grain-scale");
export const applyHazeStrength = applyScale("--haze-scale");

function parseEffectSettings(raw: string | null): EffectSettings {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw ?? "{}");
  } catch {
    return {};
  }
  const stored = (parsed && typeof parsed === "object" ? parsed : {}) as Record<string, { fade?: unknown } | null>;
  const settings: EffectSettings = {};
  for (const effect of HEADER_EFFECTS) {
    const fade = stored[effect]?.fade;
    if (effect !== "none" && inRange(fade, FADES)) settings[effect] = { fade };
  }
  return settings;
}

let effectSettingsRead: [string | null, EffectSettings] = [null, {}];

function readEffectSettings(fallback: EffectSettings = {}): EffectSettings {
  try {
    const raw = localStorage.getItem(EFFECTS_KEY);
    if (raw !== effectSettingsRead[0]) effectSettingsRead = [raw, parseEffectSettings(raw)];
    return effectSettingsRead[1];
  } catch {
    return fallback;
  }
}

function readMotion(fallback = true): boolean {
  try {
    return localStorage.getItem(MOTION_KEY) !== "off";
  } catch {
    return fallback;
  }
}

export function readFont(fallback: ReadingFont = "sans"): ReadingFont {
  try {
    const stored = localStorage.getItem(FONT_KEY);
    return stored === "serif" || stored === "hyperlegible" ? stored : "sans";
  } catch {
    return fallback;
  }
}

export function writeFont(font: ReadingFont): void {
  try {
    if (font === "sans") localStorage.removeItem(FONT_KEY);
    else localStorage.setItem(FONT_KEY, font);
  } catch {
    return;
  }
}

export function applyFont(font: ReadingFont, root: HTMLElement = document.documentElement): void {
  if (font === "sans") root.removeAttribute("data-font");
  else root.setAttribute("data-font", font);
}

export function readDensity(fallback: Density = "comfortable"): Density {
  try {
    return localStorage.getItem(DENSITY_KEY) === "compact" ? "compact" : "comfortable";
  } catch {
    return fallback;
  }
}

export function writeDensity(density: Density): void {
  try {
    if (density === "comfortable") localStorage.removeItem(DENSITY_KEY);
    else localStorage.setItem(DENSITY_KEY, density);
  } catch {
    return;
  }
}

export function applyDensity(density: Density, root: HTMLElement = document.documentElement): void {
  if (density === "comfortable") root.removeAttribute("data-density");
  else root.setAttribute("data-density", density);
}

function appearanceStore<T>(read: (fallback?: T) => T, write: (value: T) => void, apply: (value: T) => void) {
  const listeners = new Set<() => void>();
  let value: T;
  const getSnapshot = () => {
    if (!listeners.size) value = read(value);
    return value;
  };
  const subscribe = (listener: () => void) => {
    if (!listeners.size) value = read(value);
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  };
  const set = (next: T) => {
    value = next;
    write(next);
    apply(next);
    listeners.forEach((listener) => listener());
  };
  return { getSnapshot, subscribe, set };
}

const themeStore = appearanceStore(readTheme, writeTheme, (theme) => { applyTheme(theme); applyCustomPick(); });
const pickStore = (mode: ThemeMode) => appearanceStore<ThemePick>(() => readPick(mode), (pick) => writePick(mode, pick), (pick) => { applyPick(mode, pick); applyCustomPick(); });
const lightPickStore = pickStore("light");
const darkPickStore = pickStore("dark");
const pickStores = { light: lightPickStore, dark: darkPickStore };

function customThemesStore() {
  const listeners = new Set<() => void>();
  let raw: string | null = null;
  let themes: CustomTheme[] = [];
  let memory: CustomTheme[] | null = null;
  const getSnapshot = () => {
    if (memory) return memory;
    const stored = readCustomThemesRaw();
    if (stored !== raw) [raw, themes] = [stored, parseCustomThemes(stored)];
    return themes;
  };
  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  };
  const set = (next: CustomTheme[]) => {
    writeCustomThemes(next);
    memory = readCustomThemesRaw() === (next.length ? JSON.stringify(next) : null) ? null : next;
    listeners.forEach((listener) => listener());
  };
  return { getSnapshot, subscribe, set };
}

const customStore = customThemesStore();
const densityStore = appearanceStore(readDensity, writeDensity, applyDensity);
const fontStore = appearanceStore(readFont, writeFont, applyFont);
const effectStore = appearanceStore(readHeaderEffect, (effect) => writeStored(EFFECT_KEY, effect === "haze" ? null : effect), () => {});
const effectSettingsStore = appearanceStore(readEffectSettings, (settings) => writeStored(EFFECTS_KEY, Object.keys(settings).length ? JSON.stringify(settings) : null), () => {});
const motionStore = appearanceStore(readMotion, (motion) => writeStored(MOTION_KEY, motion ? null : "off"), () => {});
const textSizeStore = appearanceStore(readPercent(TEXT_SIZE_KEY, TEXT_SIZES), writePercent(TEXT_SIZE_KEY), () => {});
const grainStore = appearanceStore(readGrain, writePercent(GRAIN_KEY), applyGrain);
const hazeStrengthStore = appearanceStore(readHazeStrength, writePercent(HAZE_STRENGTH_KEY), applyHazeStrength);

function mediaQuery(media: string) {
  return {
    matches: () => window.matchMedia?.(media).matches ?? false,
    subscribe: (listener: () => void) => {
      const query = window.matchMedia?.(media);
      query?.addEventListener("change", listener);
      return () => query?.removeEventListener("change", listener);
    },
  };
}

const systemDark = mediaQuery(DARK_QUERY);
const reducedMotion = mediaQuery(REDUCED_MOTION_QUERY);

function resolvedMode(): ThemeMode {
  const theme = themeStore.getSnapshot();
  return theme === "system" ? (systemDark.matches() ? "dark" : "light") : theme;
}

/** Custom tokens sit inline on the document, above every theme block, so they are set only while their mode shows. */
export function applyCustomPick(): void {
  const pick = pickStores[resolvedMode()].getSnapshot();
  applyCustomTheme(isCustom(pick) ? customStore.getSnapshot().find((theme) => theme.id === pick) ?? null : null);
}

systemDark.subscribe(applyCustomPick);

export function useTheme() {
  const theme: Theme = useSyncExternalStore(themeStore.subscribe, themeStore.getSnapshot);
  const dark = useSyncExternalStore(systemDark.subscribe, systemDark.matches);
  const lightPick = useSyncExternalStore(lightPickStore.subscribe, lightPickStore.getSnapshot);
  const darkPick = useSyncExternalStore(darkPickStore.subscribe, darkPickStore.getSnapshot);
  return {
    theme, setTheme: themeStore.set, resolvedTheme: theme === "system" ? (dark ? "dark" : "light") : theme,
    lightPick, setLightPick: lightPickStore.set, darkPick, setDarkPick: darkPickStore.set,
  };
}

/** Saving picks the theme in the mode its ground makes, and releases a pick it held in the other mode. */
export function useCustomThemes() {
  const customThemes = useSyncExternalStore(customStore.subscribe, customStore.getSnapshot);
  const save = (theme: CustomTheme) => {
    const others = customStore.getSnapshot().filter((saved) => saved.id !== theme.id);
    const at = customStore.getSnapshot().findIndex((saved) => saved.id === theme.id);
    customStore.set(at < 0 ? [...others, theme] : others.toSpliced(at, 0, theme));
    const mode = deriveTheme(theme).mode;
    const other = mode === "dark" ? "light" : "dark";
    if (pickStores[other].getSnapshot() === theme.id) pickStores[other].set("lotus");
    pickStores[mode].set(theme.id);
  };
  const remove = (id: CustomTheme["id"]) => {
    customStore.set(customStore.getSnapshot().filter((saved) => saved.id !== id));
    for (const store of Object.values(pickStores)) if (store.getSnapshot() === id) store.set("lotus");
  };
  return { customThemes, saveCustomTheme: save, deleteCustomTheme: remove };
}

export function useReadingFont() {
  return { font: useSyncExternalStore(fontStore.subscribe, fontStore.getSnapshot), setFont: fontStore.set };
}

export function useDensity() {
  return { density: useSyncExternalStore(densityStore.subscribe, densityStore.getSnapshot), setDensity: densityStore.set };
}

export function useHeaderEffect() {
  return { effect: useSyncExternalStore(effectStore.subscribe, effectStore.getSnapshot), setEffect: effectStore.set };
}

export function useFade(effect: BandEffect) {
  const settings = useSyncExternalStore(effectSettingsStore.subscribe, effectSettingsStore.getSnapshot);
  const setFade = (fade: number) => {
    const next = { ...effectSettingsStore.getSnapshot() };
    if (fade === DEFAULT_FADE) delete next[effect];
    else next[effect] = { fade };
    effectSettingsStore.set(next);
  };
  return { fade: settings[effect]?.fade ?? DEFAULT_FADE, setFade };
}

export function useMotion() {
  const reduced = useSyncExternalStore(reducedMotion.subscribe, reducedMotion.matches);
  return { motion: useSyncExternalStore(motionStore.subscribe, motionStore.getSnapshot), reduced, setMotion: motionStore.set };
}

export function useTextSize() {
  return { textSize: useSyncExternalStore(textSizeStore.subscribe, textSizeStore.getSnapshot), setTextSize: textSizeStore.set };
}

export function useGrain() {
  return { grain: useSyncExternalStore(grainStore.subscribe, grainStore.getSnapshot), setGrain: grainStore.set };
}

export function useHazeStrength() {
  return { hazeStrength: useSyncExternalStore(hazeStrengthStore.subscribe, hazeStrengthStore.getSnapshot), setHazeStrength: hazeStrengthStore.set };
}

export function resetAppearance(): void {
  themeStore.set("system");
  lightPickStore.set("lotus");
  darkPickStore.set("lotus");
  effectStore.set("haze");
  effectSettingsStore.set({});
  motionStore.set(true);
  textSizeStore.set(FULL);
  grainStore.set(FULL);
  hazeStrengthStore.set(FULL);
  fontStore.set("sans");
  densityStore.set("comfortable");
}
