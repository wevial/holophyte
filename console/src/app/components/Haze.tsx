import { useEffect, useRef, useState } from "react";
import { mount, PALETTES, type HazeHandle } from "../../vendor/spectra/haze";
import { readMotion, useTheme } from "../appearance";

const OPTIONS = { warp: 0.4, grain: 0.05, fadeBottom: 0.9, fadeSide: 0.55, speed: 0.8 };

function themePaint(palette: string): { palette: string; intensity: number } {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
  PALETTES[palette] = [token("--haze-ground"), token("--haze-back"), token("--haze-front")];
  return { palette, intensity: Number(token("--haze-strength")) };
}

export function Haze() {
  const band = useRef<HTMLDivElement>(null);
  const live = useRef<HazeHandle | null>(null);
  const [fallback, setFallback] = useState(false);
  const { resolvedTheme, lightPick, darkPick } = useTheme();
  const palette = `${resolvedTheme === "dark" ? darkPick : lightPick}-${resolvedTheme}`;

  useEffect(() => {
    const container = band.current!;
    const haze = mount(container, { ...OPTIONS, ...themePaint(palette), motion: readMotion() });
    const canvas = container.querySelector("canvas");
    if (!canvas) {
      setFallback(true);
      return;
    }
    live.current = haze;
    const lost = () => {
      live.current = null;
      haze.destroy();
      setFallback(true);
    };
    canvas.addEventListener("webglcontextlost", lost, { once: true });
    return () => {
      canvas.removeEventListener("webglcontextlost", lost);
      live.current = null;
      haze.destroy();
    };
  }, []);

  useEffect(() => {
    if (live.current) Object.assign(live.current.config, themePaint(palette));
  }, [palette]);

  return (
    <div
      ref={band}
      aria-hidden="true"
      data-haze=""
      className={`haze-band absolute inset-x-0 top-0 -z-10 h-[188px] overflow-hidden${fallback ? " haze-fallback" : ""}`}
    />
  );
}
