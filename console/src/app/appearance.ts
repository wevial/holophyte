import { useSyncExternalStore } from "react";
import {
  applyCustomTheme, applyPick, applyTheme, deriveTheme, isCustom, parseCustomThemes, readCustomThemesRaw, readPick, readTheme, writeCustomThemes, writePick, writeTheme,
  type CustomTheme, type Theme, type ThemeMode, type ThemePick,
} from "../lib/theme";
import { KNOBS, validKnob, type KnobValue } from "./effectKnobs";

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

type EffectEntry = Record<string, KnobValue>;

type EffectSettings = Partial<Record<BandEffect, EffectEntry>>;

export const TEXT_SIZES = { min: 90, max: 125, step: 5 };
export const STRENGTHS = { min: 0, max: 200, step: 5 };
export const FULL = 100;
export const FADES = { min: 0, max: 100, step: 5 };
export const DEFAULT_FADE = 70;
export const OPACITIES = { min: 0, max: 100, step: 5 };

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
  typeof value === "number" && Number.isInteger(value) && value >= range.min && value <= range.max && value % range.step === 0;

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

function validEntryValue(effect: BandEffect, key: string, value: unknown): value is KnobValue {
  if (key === "fade") return inRange(value, FADES);
  if (key === "opacity") return inRange(value, OPACITIES);
  return validKnob(effect, key, value);
}

function parseEffectSettings(raw: string | null): EffectSettings {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw ?? "{}");
  } catch {
    return {};
  }
  const stored = (parsed && typeof parsed === "object" ? parsed : {}) as Record<string, unknown>;
  const settings: EffectSettings = {};
  for (const effect of HEADER_EFFECTS) {
    const entry = stored[effect];
    if (effect === "none" || !entry || typeof entry !== "object") continue;
    const kept = Object.entries(entry).filter(([key, value]) => validEntryValue(effect, key, value));
    if (kept.length) settings[effect] = Object.fromEntries(kept);
  }
  return settings;
}

type UnsavedEntries = Map<BandEffect, EffectEntry | null>;

let effectSettingsRead: [string | null, EffectSettings] = [null, {}];
let unsavedEntries: UnsavedEntries = new Map();
let effectSettingsShown: [EffectSettings, UnsavedEntries, EffectSettings] = [{}, unsavedEntries, {}];

function readStoredEffectSettings(): EffectSettings {
  const raw = localStorage.getItem(EFFECTS_KEY);
  if (raw !== effectSettingsRead[0]) effectSettingsRead = [raw, parseEffectSettings(raw)];
  return effectSettingsRead[1];
}

function readEffectSettings(fallback: EffectSettings = {}): EffectSettings {
  let stored: EffectSettings;
  try {
    stored = readStoredEffectSettings();
  } catch {
    return fallback;
  }
  if (!unsavedEntries.size) return stored;
  if (effectSettingsShown[0] !== stored || effectSettingsShown[1] !== unsavedEntries) {
    const shown = { ...stored };
    for (const [effect, entry] of unsavedEntries) {
      if (entry === null) delete shown[effect];
      else shown[effect] = entry;
    }
    effectSettingsShown = [stored, unsavedEntries, shown];
  }
  return effectSettingsShown[2];
}

const sameEntry = (one: EffectEntry | undefined, other: EffectEntry | undefined) => JSON.stringify(one ?? null) === JSON.stringify(other ?? null);

function writeEffectSettings(settings: EffectSettings): void {
  const raw = Object.keys(settings).length ? JSON.stringify(settings) : null;
  writeStored(EFFECTS_KEY, raw);
  let stored: EffectSettings = {};
  try {
    stored = readStoredEffectSettings();
  } catch {
    stored = {};
  }
  const unsaved: UnsavedEntries = new Map();
  for (const effect of HEADER_EFFECTS) {
    if (effect === "none") continue;
    if (!sameEntry(settings[effect], stored[effect])) unsaved.set(effect, settings[effect] ?? null);
  }
  unsavedEntries = unsaved;
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
  const reload = () => {
    value = read(value);
    listeners.forEach((listener) => listener());
  };
  return { getSnapshot, subscribe, set, reload };
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
const effectSettingsStore = appearanceStore(readEffectSettings, writeEffectSettings, () => {});
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

window.addEventListener("storage", (event) => {
  if (event.key === EFFECTS_KEY || event.key === null) effectSettingsStore.reload();
});

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

function knobDefaults(effect: BandEffect): EffectEntry {
  return { fade: DEFAULT_FADE, opacity: FULL, ...Object.fromEntries(KNOBS[effect].map((knob) => [knob.key, knob.initial])) };
}

/** An effect's Fade, Opacity and knobs over their defaults, with Haze's Strength; only values off their default are saved. */
export function useEffectSettings(effect: BandEffect) {
  const settings = useSyncExternalStore(effectSettingsStore.subscribe, effectSettingsStore.getSnapshot);
  const { hazeStrength, setHazeStrength } = useHazeStrength();
  const defaults = knobDefaults(effect);
  const saved = settings[effect];
  const set = (key: string, value: KnobValue) => {
    const next = { ...effectSettingsStore.getSnapshot() };
    const entry = { ...next[effect], [key]: value };
    if (value === defaults[key]) delete entry[key];
    if (Object.keys(entry).length) next[effect] = entry;
    else delete next[effect];
    effectSettingsStore.set(next);
  };
  const changed = Object.entries(saved ?? {}).some(([key, value]) => value !== defaults[key]) || (effect === "haze" && hazeStrength !== FULL);
  const reset = () => {
    const next = { ...effectSettingsStore.getSnapshot() };
    delete next[effect];
    effectSettingsStore.set(next);
    if (effect === "haze") setHazeStrength(FULL);
  };
  return { values: { ...defaults, ...saved }, set, changed, reset };
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
