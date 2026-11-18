import { useEffect, useRef, useState, type CSSProperties } from "react";
import { destroyStages } from "../../vendor/spectra/core/stage";
import * as caustics from "../../vendor/spectra/caustics";
import * as haze from "../../vendor/spectra/haze";
import * as hyphae from "../../vendor/spectra/hyphae";
import * as lava from "../../vendor/spectra/lava";
import * as plume from "../../vendor/spectra/plume";
import * as ribbons from "../../vendor/spectra/ribbons";
import { useCustomThemes, useFade, useHazeStrength, useMotion, useTheme, type BandEffect } from "../appearance";

interface Handle {
  config: object;
  destroy(): void;
}

const SPECTRA = { caustics, hyphae, lava, plume, ribbons };

const OPTIONS = { warp: 0.4, grain: 0.05, fadeBottom: 0.9, fadeSide: 0.55, speed: 0.8 };

function themePaint(palette: string): { palette: string; intensity: number } {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
  const colours: [string, string, string] = [token("--haze-ground"), token("--haze-back"), token("--haze-front")];
  // A static frame redraws only when the config changes, so the palette's name carries its colours.
  const named = `${palette}:${colours.join(":")}`;
  haze.PALETTES[named] = colours;
  return { palette: named, intensity: Number(token("--haze-strength")) * Number(token("--haze-scale") || 1) };
}

function mountEffect(effect: BandEffect, container: HTMLElement, palette: string, motion: boolean): Handle {
  if (effect === "haze") return haze.mount(container, { ...OPTIONS, ...themePaint(palette), motion });
  return SPECTRA[effect].mount(container, { motion });
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
  const { fade } = useFade(effect);
  const tall = effect === "lava";

  useEffect(() => {
    if (!dark) return;
    const container = (tall ? floored : band).current!;
    let drawing: Handle;
    try {
      drawing = mountEffect(effect, container, palette, motion);
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
    if (live.current) Object.assign(live.current.config, effect === "haze" ? themePaint(palette) : {}, { motion });
  }, [palette, hazeStrength, motion, customThemes]);

  // A renderer that fell back under a slow moving frame gets another try at its one still frame.
  useEffect(() => {
    if (still && !live.current) setAttempt((count) => count + 1);
  }, [still]);

  return (
    <div
      ref={band}
      aria-hidden="true"
      data-haze=""
      style={dark ? { "--haze-fade": `${fade}%` } as CSSProperties : undefined}
      className={`haze-band absolute inset-x-0 top-0 -z-10 h-[188px] overflow-hidden${!dark || fallback ? " haze-fallback" : ""}`}
    >
      {/* Lava pools its wax at the bottom of its box, so the box runs past the band's clipped edge. */}
      {tall && <div ref={floored} style={{ position: "absolute", insetInline: 0, top: 0, height: "135%" }} />}
    </div>
  );
}
