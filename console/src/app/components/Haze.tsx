import { useEffect, useRef, useState, type CSSProperties } from "react";
import { destroyStages } from "../../vendor/spectra/core/stage";
import * as caustics from "../../vendor/spectra/caustics";
import * as haze from "../../vendor/spectra/haze";
import * as hyphae from "../../vendor/spectra/hyphae";
import * as lava from "../../vendor/spectra/lava";
import * as plume from "../../vendor/spectra/plume";
import * as ribbons from "../../vendor/spectra/ribbons";
import { useCustomThemes, useEffectSettings, useMotion, useTheme, type BandEffect } from "../appearance";
import { HAZE_TUNING, KNOBS, paletteKnob, THEME, themePaint, themePalette, type KnobValue, type ThemeColours } from "../effectKnobs";

interface Handle {
  config: object;
  advance?(seconds: number): void;
  reset?(): void;
  destroy(): void;
}

const LAVA_RESETTLE_S = 3;
const PLUME_RESETTLE_S = 2;
const RESETTLE_AFTER_MS = 250;

/** A held frame shows no motion, so a knob that only changes how the simulation moves re-runs it for the frame. */
function resettle(effect: BandEffect, drawing: Handle, changed: (key: string) => boolean): void {
  if (effect === "lava" && changed("heat")) drawing.advance?.(LAVA_RESETTLE_S);
  if (effect === "plume" && changed("currents")) drawing.advance?.(PLUME_RESETTLE_S);
  if (effect === "hyphae" && ["appetite", "food", "seed"].some(changed)) drawing.reset?.();
}

export const LAVA_BOX = 4.73;

const SPECTRA = { caustics, hyphae, lava, plume, ribbons };

const HAZE_EDGES = { fadeBottom: 0.9, fadeSide: 0.55 };

const PALETTES = { haze: haze.PALETTES, caustics: caustics.PALETTES, lava: lava.PALETTES, ribbons: ribbons.PALETTES, hyphae: hyphae.PALETTES } as Record<Exclude<BandEffect, "plume">, Record<string, string[]>>;

const token = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function themeColours(): ThemeColours {
  return { ground: token("--haze-ground"), back: token("--haze-back"), front: token("--haze-front"), accent: token("--accent") };
}

/** Registers the theme's palette for `effect`; a still frame redraws only when the config changes, so the name carries the colours. */
function registerTheme(effect: BandEffect, pick: string): string {
  const colours = themeColours();
  if (effect === "plume") {
    const paint = themePaint(colours);
    const named = `${THEME}-${pick}:${[...paint.inks, paint.ground].join(":")}`;
    plume.PAINTS[named] = paint;
    return named;
  }
  const palette = themePalette(effect, colours);
  const named = `${THEME}-${pick}:${palette.join(":")}`;
  PALETTES[effect][named] = palette;
  return named;
}

function liveOptions(effect: BandEffect, values: Record<string, KnobValue>, pick: string): Record<string, KnobValue> {
  const options = Object.fromEntries(KNOBS[effect].map((knob) => [knob.key, values[knob.key]!]));
  const palette = paletteKnob(effect).key;
  if (options[palette] === THEME) options[palette] = registerTheme(effect, pick);
  return effect === "haze" ? { ...options, intensity: Number(token("--haze-strength")) * Number(token("--haze-scale") || 1) } : options;
}

function mountEffect(effect: BandEffect, container: HTMLElement, options: Record<string, KnobValue>, motion: boolean): Handle {
  if (effect === "haze") return haze.mount(container, { ...HAZE_TUNING, ...HAZE_EDGES, ...options, motion });
  return SPECTRA[effect].mount(container, { ...options, motion } as never);
}

export function Haze({ effect }: { effect: BandEffect }) {
  const band = useRef<HTMLDivElement>(null);
  const floored = useRef<HTMLDivElement>(null);
  const live = useRef<Handle | null>(null);
  const [fallback, setFallback] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { resolvedTheme, lightPick, darkPick } = useTheme();
  const dark = resolvedTheme === "dark";
  const palette = `${dark ? darkPick : lightPick}-${resolvedTheme}`;
  const { motion, reduced } = useMotion();
  const still = !motion || reduced;
  const { customThemes } = useCustomThemes();
  const { values } = useEffectSettings(effect);
  const applied = useRef(values);
  const resettling = useRef<ReturnType<typeof setTimeout>>(undefined);
  const knobs = JSON.stringify(values);
  const tall = effect === "lava";

  useEffect(() => {
    if (!dark) return;
    const container = (tall ? floored : band).current!;
    let drawing: Handle;
    try {
      drawing = mountEffect(effect, container, liveOptions(effect, values, palette), motion);
    } catch {
      destroyStages(container);
      container.querySelector("canvas")?.remove();
      setFallback(true);
      return;
    }
    const canvas = container.querySelector("canvas");
    if (!canvas) {
      setFallback(true);
      return;
    }
    live.current = drawing;
    applied.current = values;
    if (still && effect === "plume") drawing.advance?.(PLUME_RESETTLE_S);
    setFallback(false);
    const lost = () => {
      live.current = null;
      drawing.destroy();
      setFallback(true);
    };
    canvas.addEventListener("webglcontextlost", lost, { once: true });
    return () => {
      canvas.removeEventListener("webglcontextlost", lost);
      live.current = null;
      drawing.destroy();
    };
  }, [dark, effect, attempt]);

  useEffect(() => {
    if (!live.current) return;
    Object.assign(live.current.config, liveOptions(effect, values, palette), { motion });
    clearTimeout(resettling.current);
    if (!still) {
      applied.current = values;
      return;
    }
    const drawing = live.current;
    // A slider drag sends a burst of values; the simulation re-runs once, for the last.
    resettling.current = setTimeout(() => {
      if (live.current !== drawing) return;
      const before = applied.current;
      applied.current = values;
      resettle(effect, drawing, (key) => before[key] !== values[key]);
    }, RESETTLE_AFTER_MS);
  }, [palette, motion, still, customThemes, knobs]);

  useEffect(() => () => clearTimeout(resettling.current), []);

  // A renderer that fell back under a slow moving frame gets another try at its one still frame.
  useEffect(() => {
    if (still && !live.current) setAttempt((count) => count + 1);
  }, [still]);

  return (
    <div
      ref={band}
      aria-hidden="true"
      data-haze=""
      style={{ ...(dark && { "--haze-fade": `${values.fade}%` }), "--haze-scale": effect === "haze" ? Number(values.strength) / 100 : 1, opacity: Number(values.opacity) / 100 } as CSSProperties}
      className={`haze-band absolute inset-x-0 top-0 -z-10 h-[188px] overflow-hidden${!dark || fallback ? " haze-fallback" : ""}`}
    >
      {/* Lava pools its wax at the bottom of its box, so the box runs past the band's clipped edge. */}
      {tall && <div ref={floored} style={{ position: "absolute", insetInline: 0, top: 0, height: `${Math.round(LAVA_BOX * 100)}%` }} />}
    </div>
  );
}
