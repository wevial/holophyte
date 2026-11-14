import { useSyncExternalStore } from "react";
import { applyPick, applyTheme, readPick, readTheme, writePick, writeTheme, type Theme, type ThemeMode, type ThemePick } from "../lib/theme";

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
const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

export type HeaderEffect = "haze" | "none";

export const TEXT_SIZES = { min: 90, max: 125, step: 5 };
export const STRENGTHS = { min: 0, max: 200, step: 5 };
export const FULL = 100;

function readHeaderEffect(fallback: HeaderEffect = "haze"): HeaderEffect {
  try {
    return localStorage.getItem(EFFECT_KEY) === "none" ? "none" : "haze";
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

function readPercent(key: string, range: typeof STRENGTHS) {
  return (fallback = FULL): number => {
    try {
      const stored = Number(localStorage.getItem(key) ?? FULL);
      return Number.isInteger(stored) && stored >= range.min && stored <= range.max && stored % range.step === 0 ? stored : FULL;
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

export const readGrain = readPercent(GRAIN_KEY, STRENGTHS);
export const readHazeStrength = readPercent(HAZE_STRENGTH_KEY, STRENGTHS);
export const applyGrain = applyScale("--grain-scale");
export const applyHazeStrength = applyScale("--haze-scale");

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

const themeStore = appearanceStore(readTheme, writeTheme, applyTheme);
const pickStore = (mode: ThemeMode) => appearanceStore<ThemePick>(() => readPick(mode), (pick) => writePick(mode, pick), (pick) => applyPick(mode, pick));
const lightPickStore = pickStore("light");
const darkPickStore = pickStore("dark");
const densityStore = appearanceStore(readDensity, writeDensity, applyDensity);
const fontStore = appearanceStore(readFont, writeFont, applyFont);
const effectStore = appearanceStore(readHeaderEffect, (effect) => writeStored(EFFECT_KEY, effect === "haze" ? null : effect), () => {});
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

export function useReadingFont() {
  return { font: useSyncExternalStore(fontStore.subscribe, fontStore.getSnapshot), setFont: fontStore.set };
}

export function useDensity() {
  return { density: useSyncExternalStore(densityStore.subscribe, densityStore.getSnapshot), setDensity: densityStore.set };
}

export function useHeaderEffect() {
  return { effect: useSyncExternalStore(effectStore.subscribe, effectStore.getSnapshot), setEffect: effectStore.set };
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
  motionStore.set(true);
  textSizeStore.set(FULL);
  grainStore.set(FULL);
  hazeStrengthStore.set(FULL);
  fontStore.set("sans");
  densityStore.set("comfortable");
}
