import { destroyStages } from "../../console/src/vendor/spectra/core/stage";
import * as haze from "../../console/src/vendor/spectra/haze";

type Media = (query: string) => MediaQueryList;

const OPTIONS = { warp: 0.4, grain: 0.05, fadeBottom: 0.9, fadeSide: 0.55, speed: 0.8 };

function themePaint(root: HTMLElement): { palette: string; intensity: number } {
  const style = getComputedStyle(root);
  const token = (name: string) => style.getPropertyValue(name).trim();
  const colours: [string, string, string] = [token("--haze-ground"), token("--haze-back"), token("--haze-front")];
  const named = `landing:${colours.join(":")}`;
  haze.PALETTES[named] = colours;
  return { palette: named, intensity: Number(token("--haze-strength")) };
}

/** Draws Haze over the band's gradient while the root is in Night; returns a function that stops following the theme and removes the drawing. */
export function mountHeader(band: HTMLElement, root: HTMLElement, media: Media): () => void {
  const dark = media("(prefers-color-scheme: dark)");
  const reduced = media("(prefers-reduced-motion: reduce)");
  let live: haze.HazeHandle | null = null;
  let failed = false;
  let unlisten = () => {};

  const night = () => {
    const theme = root.getAttribute("data-theme");
    return theme === "dark" || (theme !== "light" && dark.matches);
  };

  function drop() {
    unlisten();
    unlisten = () => {};
    live?.destroy();
    live = null;
  }

  function lose() {
    drop();
    failed = true;
  }

  function draw() {
    failed = true;
    let drawing: haze.HazeHandle;
    try {
      drawing = haze.mount(band, { ...OPTIONS, ...themePaint(root), motion: !reduced.matches });
    } catch {
      destroyStages(band);
      band.querySelector("canvas")?.remove();
      return;
    }
    const canvas = band.querySelector("canvas");
    if (!canvas) return;
    live = drawing;
    failed = false;
    canvas.addEventListener("webglcontextlost", lose, { once: true });
    unlisten = () => canvas.removeEventListener("webglcontextlost", lose);
  }

  const sync = () => {
    if (!night()) {
      drop();
      failed = false;
    } else if (!live && !failed) draw();
  };
  const still = () => {
    if (live) live.config.motion = !reduced.matches;
  };

  const observer = new MutationObserver(sync);
  observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  dark.addEventListener("change", sync);
  reduced.addEventListener("change", still);
  sync();
  return () => {
    observer.disconnect();
    dark.removeEventListener("change", sync);
    reduced.removeEventListener("change", still);
    drop();
  };
}

const band = document.currentScript && document.querySelector<HTMLElement>(".haze");
if (band) mountHeader(band, document.documentElement, (query) => matchMedia(query));
