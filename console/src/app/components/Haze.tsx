import { useEffect, useRef, useState } from "react";
import * as caustics from "../../vendor/spectra/caustics";
import * as haze from "../../vendor/spectra/haze";
import * as lava from "../../vendor/spectra/lava";
import * as plume from "../../vendor/spectra/plume";
import * as ribbons from "../../vendor/spectra/ribbons";
import { useCustomThemes, useHazeStrength, useMotion, useTheme, type HeaderEffect } from "../appearance";

type BandEffect = Exclude<HeaderEffect, "none">;

interface Handle {
  config: object;
  destroy(): void;
}

const SPECTRA = { caustics, lava, plume, ribbons };

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

function paint(effect: BandEffect, palette: string) {
  return effect === "haze" ? themePaint(palette) : {};
}

function mountEffect(effect: BandEffect, container: HTMLElement, palette: string, motion: boolean): Handle {
  if (effect === "haze") return haze.mount(container, { ...OPTIONS, ...themePaint(palette), motion });
  return SPECTRA[effect].mount(container, { motion });
}

export function Haze({ effect }: { effect: BandEffect }) {
  const band = useRef<HTMLDivElement>(null);
  const live = useRef<Handle | null>(null);
  const [fallback, setFallback] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { resolvedTheme, lightPick, darkPick } = useTheme();
  const dark = resolvedTheme === "dark";
  const palette = `${dark ? darkPick : lightPick}-${resolvedTheme}`;
  const { hazeStrength } = useHazeStrength();
  const { motion } = useMotion();
  const { customThemes } = useCustomThemes();

  useEffect(() => {
    if (!dark) return;
    const container = band.current!;
    let drawing: Handle;
    try {
      drawing = mountEffect(effect, container, palette, motion);
    } catch {
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
    if (live.current) Object.assign(live.current.config, paint(effect, palette), { motion });
  }, [palette, hazeStrength, motion, customThemes]);

  // A renderer that fell back under a slow moving frame gets another try at its one still frame.
  useEffect(() => {
    if (!motion && !live.current) setAttempt((count) => count + 1);
  }, [motion]);

  return (
    <div
      ref={band}
      aria-hidden="true"
      data-haze=""
      className={`haze-band absolute inset-x-0 top-0 -z-10 h-[188px] overflow-hidden${!dark || fallback ? " haze-fallback" : ""}`}
    />
  );
}
