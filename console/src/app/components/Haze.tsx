import { useEffect, useRef, useState, type CSSProperties } from "react";
import { destroyStages } from "../../vendor/spectra/core/stage";
import * as caustics from "../../vendor/spectra/caustics";
import * as haze from "../../vendor/spectra/haze";
import * as hyphae from "../../vendor/spectra/hyphae";
import * as lava from "../../vendor/spectra/lava";
import * as plume from "../../vendor/spectra/plume";
import * as ribbons from "../../vendor/spectra/ribbons";
import { useCustomThemes, useEffectSettings, useHazeStrength, useMotion, useTheme, type BandEffect } from "../appearance";
import { HAZE_TUNING, KNOBS, paletteKnob, THEME, themePaint, themePalette, type KnobValue, type ThemeColours } from "../effectKnobs";

interface Handle {
  config: object;
  destroy(): void;
}

export const LAVA_BOX = 4.73;

const SPECTRA = { caustics, hyphae, lava, plume, ribbons };

const HAZE_EDGES = { fadeBottom: 0.9, fadeSide: 0.55 };

const PALETTES = { haze: haze.PALETTES, caustics: caustics.PALETTES, lava: lava.PALETTES, ribbons: ribbons.PALETTES, hyphae: hyphae.PALETTES } as Record<Exclude<BandEffect, "plume">, Record<string, string[]>>;

function themeColours(): ThemeColours {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
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

function hazeIntensity(): number {
  const style = getComputedStyle(document.documentElement);
  return Number(style.getPropertyValue("--haze-strength").trim()) * Number(style.getPropertyValue("--haze-scale").trim() || 1);
}

function liveOptions(effect: BandEffect, values: Record<string, KnobValue>, pick: string): Record<string, KnobValue> {
  const options = Object.fromEntries(KNOBS[effect].map((knob) => [knob.key, values[knob.key]!]));
  const palette = paletteKnob(effect).key;
  if (options[palette] === THEME) options[palette] = registerTheme(effect, pick);
  return effect === "haze" ? { ...options, intensity: hazeIntensity() } : options;
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
  const { hazeStrength } = useHazeStrength();
  const { motion, reduced } = useMotion();
  const still = !motion || reduced;
  const { customThemes } = useCustomThemes();
  const { values } = useEffectSettings(effect);
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
    if (live.current) Object.assign(live.current.config, liveOptions(effect, values, palette), { motion });
  }, [palette, hazeStrength, motion, customThemes, knobs]);

  // A renderer that fell back under a slow moving frame gets another try at its one still frame.
  useEffect(() => {
    if (still && !live.current) setAttempt((count) => count + 1);
  }, [still]);

  return (
    <div
      ref={band}
      aria-hidden="true"
      data-haze=""
      style={dark ? { "--haze-fade": `${values.fade}%`, opacity: Number(values.opacity) / 100 } as CSSProperties : undefined}
      className={`haze-band absolute inset-x-0 top-0 -z-10 h-[188px] overflow-hidden${!dark || fallback ? " haze-fallback" : ""}`}
    >
      {/* Lava pools its wax at the bottom of its box, so the box runs past the band's clipped edge. */}
      {tall && <div ref={floored} style={{ position: "absolute", insetInline: 0, top: 0, height: `${Math.round(LAVA_BOX * 100)}%` }} />}
    </div>
  );
}
