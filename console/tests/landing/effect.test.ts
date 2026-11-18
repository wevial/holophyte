import { afterEach, beforeEach, expect, mock, spyOn, test } from "bun:test";
import { mountHeader } from "../../../landing/src/effect";
import * as haze from "../../src/vendor/spectra/haze";

const PAGE = await Bun.file(new URL("../../../landing/public/index.html", import.meta.url)).text();
const root = document.documentElement;

let band: HTMLElement;
let page: HTMLStyleElement;
let stop = () => {};

/** A media query lookup where only the named queries match. */
const matching = (...queries: string[]) => (query: string) =>
  ({ matches: queries.includes(query), addEventListener() {}, removeEventListener() {} }) as unknown as MediaQueryList;

/** Replaces Haze's mount with one that adds a canvas and records each call's band and options. */
function recordMounts() {
  const mounts: { band: HTMLElement; options: Partial<haze.HazeConfig>; destroyed: boolean }[] = [];
  spyOn(haze, "mount").mockImplementation((container: HTMLElement, options = {}) => {
    const canvas = container.appendChild(document.createElement("canvas"));
    const record = { band: container, options, destroyed: false };
    mounts.push(record);
    return { config: { ...options } as haze.HazeConfig, destroy: () => { record.destroyed = true; canvas.remove(); } };
  });
  return mounts;
}

beforeEach(() => {
  page = document.head.appendChild(document.createElement("style"));
  page.textContent = /<style>([\s\S]*?)<\/style>/.exec(PAGE)![1]!;
  band = document.body.appendChild(document.createElement("div"));
  band.className = "haze";
  root.setAttribute("data-theme", "dark");
});
afterEach(() => {
  stop();
  mock.restore();
  stop = () => {};
  band.remove();
  page.remove();
  root.removeAttribute("data-theme");
});

test("Night mounts Haze once into the band with the console's options, the Lotus haze colours and intensity 0.30, in motion", () => {
  const mounts = recordMounts();
  stop = mountHeader(band, root, matching("(prefers-color-scheme: dark)"));
  expect(mounts.length).toBe(1);
  const { band: into, options } = mounts[0]!;
  expect(into).toBe(band);
  expect(options).toMatchObject({ warp: 0.4, grain: 0.05, fadeBottom: 0.9, fadeSide: 0.55, speed: 0.8, intensity: 0.3, motion: true });
  expect(haze.PALETTES[options.palette!]).toEqual(["#0d0a15", "#4b3fc4", "#c77ee8"]);
});

test("reduced motion mounts Haze with motion off", () => {
  const mounts = recordMounts();
  stop = mountHeader(band, root, matching("(prefers-reduced-motion: reduce)"));
  expect(mounts.length).toBe(1);
  expect(mounts[0]!.options.motion).toBe(false);
});

test("switching Night to Dawn destroys the drawing, and switching back mounts Haze again", async () => {
  const mounts = recordMounts();
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
  stop = mountHeader(band, root, matching());
  expect(mounts.length).toBe(1);

  root.setAttribute("data-theme", "light");
  await settle();
  expect(mounts[0]!.destroyed).toBe(true);

  root.setAttribute("data-theme", "dark");
  await settle();
  expect(mounts.length).toBe(2);
  expect(mounts[1]!.destroyed).toBe(false);
  expect(band.querySelectorAll("canvas").length).toBe(1);
});

test("a Haze mount that throws leaves the band's gradient with no canvas and does not throw", () => {
  const spy = spyOn(haze, "mount").mockImplementation((container: HTMLElement) => {
    container.appendChild(document.createElement("canvas"));
    throw new Error("shader failed");
  });
  expect(() => { stop = mountHeader(band, root, matching()); }).not.toThrow();
  expect(spy).toHaveBeenCalledTimes(1);
  expect(band.querySelector("canvas")).toBeNull();
});
