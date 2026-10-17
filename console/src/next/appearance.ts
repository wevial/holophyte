import { useSyncExternalStore } from "react";
import { applyTheme, readTheme, writeTheme, type Theme } from "../lib/theme";

export type ReadingFont = "sans" | "serif" | "hyperlegible";

export type Density = "comfortable" | "compact";

const DENSITY_KEY = "holophyte.next.density";
const FONT_KEY = "holophyte.next.font";
const DARK_QUERY = "(prefers-color-scheme: dark)";

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
const densityStore = appearanceStore(readDensity, writeDensity, applyDensity);
const fontStore = appearanceStore(readFont, writeFont, applyFont);

function systemDark(): boolean {
  return window.matchMedia?.(DARK_QUERY).matches ?? false;
}

function subscribeSystem(listener: () => void) {
  const query = window.matchMedia?.(DARK_QUERY);
  query?.addEventListener("change", listener);
  return () => query?.removeEventListener("change", listener);
}

export function useTheme() {
  const theme: Theme = useSyncExternalStore(themeStore.subscribe, themeStore.getSnapshot);
  const dark = useSyncExternalStore(subscribeSystem, systemDark);
  return { theme, setTheme: themeStore.set, resolvedTheme: theme === "system" ? (dark ? "dark" : "light") : theme };
}

export function useReadingFont() {
  return { font: useSyncExternalStore(fontStore.subscribe, fontStore.getSnapshot), setFont: fontStore.set };
}

export function useDensity() {
  return { density: useSyncExternalStore(densityStore.subscribe, densityStore.getSnapshot), setDensity: densityStore.set };
}
