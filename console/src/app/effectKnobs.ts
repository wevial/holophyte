import * as caustics from "../vendor/spectra/caustics";
import * as haze from "../vendor/spectra/haze";
import * as hyphae from "../vendor/spectra/hyphae";
import * as lava from "../vendor/spectra/lava";
import { CONFIG as NEBULA } from "../vendor/spectra/nebula";
import * as plume from "../vendor/spectra/plume";
import * as ribbons from "../vendor/spectra/ribbons";
import type { BandEffect } from "./appearance";

export type KnobValue = number | string;

interface RangeKnob { kind: "range"; key: string; label: string; min: number; max: number; step: number; initial: number }

export interface ChoiceKnob { kind: "choice"; key: string; label: string; choices: readonly string[]; initial: string }

type Knob = RangeKnob | ChoiceKnob;

export const THEME = "theme";

const range = (key: string, label: string, min: number, max: number, step: number, initial: number): RangeKnob =>
  ({ kind: "range", key, label, min, max, step, initial });

const palette = (names: string[], key = "palette"): ChoiceKnob =>
  ({ kind: "choice", key, label: "Palette", choices: [THEME, ...names], initial: THEME });

const seed = (initial: number) => range("seed", "Seed", 0, 100, 1, initial);

export const HAZE_TUNING = { ...haze.DEFAULTS, warp: 0.4, grain: 0.05, speed: 0.8 };

const PLUME_CURRENTS = 1;

export const KNOBS: Record<BandEffect, readonly Knob[]> = {
  haze: [
    palette([...Object.keys(haze.PALETTES), "rainbow"]),
    range("scale", "Size", 0.3, 3, 0.05, HAZE_TUNING.scale),
    range("warp", "Smokiness", 0, 1.5, 0.05, HAZE_TUNING.warp),
    range("speed", "Speed", 0, 5, 0.05, HAZE_TUNING.speed),
    range("grain", "Grain", 0, 0.5, 0.01, HAZE_TUNING.grain),
    seed(HAZE_TUNING.seed),
  ],
  caustics: [
    palette(Object.keys(caustics.PALETTES)),
    range("depth", "Depth", 0.1, 1.2, 0.01, caustics.DEFAULTS.depth),
    range("scale", "Size", 0.3, 3, 0.05, caustics.DEFAULTS.scale),
    range("speed", "Speed", 0, 2, 0.01, caustics.DEFAULTS.speed),
    range("intensity", "Intensity", 0, 1.5, 0.01, caustics.DEFAULTS.intensity),
    range("dispersion", "Dispersion", 0, 12, 0.1, caustics.DEFAULTS.dispersion),
    seed(caustics.DEFAULTS.seed),
  ],
  plume: [
    palette(Object.keys(plume.PAINTS), "paint"),
    range("currents", "Currents", 0, 4, 0.05, PLUME_CURRENTS),
    range("speed", "Speed", 0, 5, 0.05, NEBULA.speed),
  ],
  ribbons: [
    palette(Object.keys(ribbons.PALETTES)),
    range("count", "Count", 1, 6, 1, ribbons.DEFAULTS.count),
    range("amplitude", "Amplitude", 0, 0.8, 0.01, ribbons.DEFAULTS.amplitude),
    { kind: "choice", key: "placement", label: "Placement", choices: ["center", "full", "fill"], initial: ribbons.DEFAULTS.placement },
    range("speed", "Speed", 0, 5, 0.05, ribbons.DEFAULTS.speed),
    range("glow", "Glow", 0, 3, 0.05, ribbons.DEFAULTS.glow),
    seed(ribbons.DEFAULTS.seed),
  ],
  lava: [
    palette(["rainbow", "thermal", ...Object.keys(lava.PALETTES)]),
    range("amount", "Wax", 0.05, 0.35, 0.01, lava.DEFAULTS.amount),
    range("heat", "Heat", 0, 3, 0.05, lava.DEFAULTS.heat),
    range("speed", "Speed", 0, 5, 0.05, lava.DEFAULTS.speed),
    range("glow", "Glow", 0, 3, 0.05, lava.DEFAULTS.glow),
    seed(lava.DEFAULTS.seed),
  ],
  hyphae: [
    palette(Object.keys(hyphae.PALETTES)),
    range("food", "Patches", 1, 8, 1, hyphae.DEFAULTS.food),
    range("appetite", "Appetite", 0, 3, 0.05, hyphae.DEFAULTS.appetite),
    range("speed", "Speed", 0, 4, 0.05, hyphae.DEFAULTS.speed),
    range("glow", "Glow", 0, 3, 0.05, hyphae.DEFAULTS.glow),
    seed(hyphae.DEFAULTS.seed),
  ],
};

function onStep(value: unknown, knob: { min: number; max: number; step: number }): value is number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < knob.min || value > knob.max) return false;
  const steps = (value - knob.min) / knob.step;
  return Math.abs(steps - Math.round(steps)) < 1e-6;
}

export function validKnob(effect: BandEffect, key: string, value: unknown): value is KnobValue {
  const knob = KNOBS[effect].find((candidate) => candidate.key === key);
  if (!knob) return false;
  return knob.kind === "range" ? onStep(value, knob) : typeof value === "string" && knob.choices.includes(value);
}

export function paletteKnob(effect: BandEffect): ChoiceKnob {
  return KNOBS[effect].find((knob): knob is ChoiceKnob => knob.label === "Palette")!;
}

export interface ThemeColours { ground: string; back: string; front: string; accent: string }

function channels(hex: string): number[] {
  const n = parseInt(hex.trim().slice(1), 16);
  return [n >> 16 & 255, n >> 8 & 255, n & 255];
}

const WHITE = [255, 255, 255];

function blend(from: number[], to: number[], share: number): string {
  return `#${from.map((value, at) => Math.round(value + (to[at]! - value) * share).toString(16).padStart(2, "0")).join("")}`;
}

const mix = (colour: string, toward: string, share: number) => blend(channels(colour), channels(toward), share);

const lighten = (colour: string, share: number) => blend(channels(colour), WHITE, share);

export function themePalette(effect: Exclude<BandEffect, "plume">, { ground, back, front, accent }: ThemeColours): string[] {
  switch (effect) {
    case "haze": return [ground, back, front];
    case "caustics": return [ground, lighten(front, 0.35)];
    case "lava": return [ground, back, front];
    case "ribbons": return [back, front, accent, ...[back, front, accent].map((colour) => lighten(colour, 0.3))];
    case "hyphae": return [ground, mix(back, ground, 0.5), front, lighten(front, 0.7)];
  }
}

export function themePaint({ ground, back, front }: ThemeColours): { inks: [string, string, string, string]; ground: string } {
  return { inks: [back, mix(back, front, 0.5), front, lighten(front, 0.6)], ground };
}
