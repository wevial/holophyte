import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { applyTheme, THEME_KEY } from "../../src/lib/theme";
import * as caustics from "../../src/vendor/spectra/caustics";
import * as haze from "../../src/vendor/spectra/haze";
import * as hyphae from "../../src/vendor/spectra/hyphae";
import * as lava from "../../src/vendor/spectra/lava";
import * as plume from "../../src/vendor/spectra/plume";
import * as ribbons from "../../src/vendor/spectra/ribbons";
import type { Status } from "../../src/lib/types";
import { fakeDeps, NO_ATTENTION, settle, stubFetch } from "../harness";

const ORIGIN = "http://writer-host:7710";
const status = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;

/** Each draw to the canvas, as the viewport it covered. */
let canvasDraws: [number, number][] = [];

/** A WebGL2 context that grants float targets, compiles every shader, links every program and records each draw to its canvas. */
function grantingGl(canvas: HTMLCanvasElement) {
  let framebuffer: unknown = null;
  let viewport: [number, number] = [0, 0];
  return new Proxy({}, {
    get: (_, name) => {
      if (typeof name === "string" && /^[A-Z_0-9]+$/.test(name)) return name;
      if (name === "drawingBufferWidth") return canvas.width;
      if (name === "drawingBufferHeight") return canvas.height;
      if (name === "getProgramParameter") return (_program: unknown, pname: string) => (pname === "ACTIVE_UNIFORMS" ? 0 : true);
      if (name === "getShaderParameter") return () => true;
      if (name === "getExtension") return (extension: string) => (extension === "EXT_color_buffer_float" ? {} : null);
      if (name === "bindFramebuffer") return (_target: unknown, bound: unknown) => { framebuffer = bound; };
      if (name === "viewport") return (_x: number, _y: number, width: number, height: number) => { viewport = [width, height]; };
      if (name === "drawArrays") return () => { if (framebuffer === null) canvasDraws.push(viewport); };
      return () => ({});
    },
  });
}

let webgl2Asks = 0;

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  webgl2Asks = 0;
  canvasDraws = [];
  spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((function (this: HTMLCanvasElement, kind: string) {
    if (kind !== "webgl2") return null;
    webgl2Asks += 1;
    return grantingGl(this);
  }) as typeof HTMLCanvasElement.prototype.getContext);
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  window.location.hash = "";
  document.documentElement.removeAttribute("data-theme");
  (HTMLCanvasElement.prototype.getContext as unknown as { mockRestore: () => void }).mockRestore();
});

async function mount() {
  window.location.hash = "#/";
  const fake = fakeDeps(stubFetch({ status, attention: NO_ATTENTION }));
  render(<QueryClientProvider client={consoleQueryClient()}><ConsoleApp base={ORIGIN} pollDeps={fake.deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
}

const band = () => screen.getByRole("main").querySelector<HTMLElement>("[data-haze]")!;
const chooseTheme = (name: string) => fireEvent.click(within(screen.getByRole("group", { name: "Theme" })).getByRole("button", { name }));

/** Distinct light and dark haze tokens, since the tests run without the compiled stylesheet. */
const LIGHT = { ground: "#fbfaff", back: "#c8bff6", front: "#f3bddb" };
const DARK = { ground: "#0d0a15", back: "#4b3fc4", front: "#c77ee8" };
const TOKENS = `:root { --haze-ground: ${LIGHT.ground}; --haze-back: ${LIGHT.back}; --haze-front: ${LIGHT.front}; --accent: #5a4ebf; }
:root[data-theme="dark"] { --haze-ground: ${DARK.ground}; --haze-back: ${DARK.back}; --haze-front: ${DARK.front}; --accent: #b9a3ff; }`;

const EFFECTS = { haze, caustics, plume, ribbons, lava, hyphae };
type Given = { transparent?: boolean; palette?: string };

/** Replaces each vendored mount with one that adds a canvas and records the options it was given and the live config it returned, or throws when `throwing`. */
function fakeMounts({ throwing = false } = {}) {
  const given: Record<string, Given[]> = {};
  const configs: Record<string, Given[]> = {};
  const tokens = document.createElement("style");
  tokens.textContent = TOKENS;
  document.head.appendChild(tokens);
  const spies = Object.entries(EFFECTS).map(([name, module]) => spyOn(module, "mount").mockImplementation(((container: HTMLElement, options: Given = {}) => {
    if (throwing) throw new Error("no WebGL2");
    container.appendChild(document.createElement("canvas"));
    (given[name] ??= []).push({ ...options });
    const config = { ...options };
    (configs[name] ??= []).push(config);
    return { config, destroy: () => container.querySelector("canvas")?.remove() };
  }) as never));
  return { given, configs, restore: () => { spies.forEach((spy) => spy.mockRestore()); tokens.remove(); } };
}

/** Stores the theme, picked effect and its settings, and stamps the theme on the document as the console's entry point does before it renders. */
function stored(theme: "light" | "dark", effect: string, settings: Record<string, number> = {}) {
  localStorage.setItem(THEME_KEY, theme);
  applyTheme(theme);
  localStorage.setItem("holophyte.next.effect", effect);
  localStorage.setItem("holophyte.effects", JSON.stringify({ [effect]: settings }));
}

const hasFallback = () => band().classList.contains("haze-fallback");

test("a stored light theme mounts Haze once, over the page, with a Theme palette of the light haze ground, back and front", async () => {
  const mounts = fakeMounts();
  try {
    stored("light", "haze");
    await mount();
    const [options] = mounts.given.haze!;
    expect([mounts.given.haze!.length, options!.transparent, hasFallback()]).toEqual([1, true, false]);
    expect(haze.PALETTES[options!.palette!]).toEqual([LIGHT.ground, LIGHT.back, LIGHT.front]);
  } finally {
    cleanup();
    mounts.restore();
  }
});

test("each of the six effects mounts over the page in a dark theme and in a light theme", async () => {
  const mounts = fakeMounts();
  try {
    const seen: unknown[][] = [];
    for (const theme of ["dark", "light"] as const) {
      for (const effect of Object.keys(EFFECTS)) {
        stored(theme, effect);
        await mount();
        seen.push([theme, effect, mounts.given[effect]?.at(-1)?.transparent]);
        cleanup();
        delete mounts.given[effect];
      }
    }
    expect(seen).toEqual(["dark", "light"].flatMap((theme) => Object.keys(EFFECTS).map((effect) => [theme, effect, true])));
  } finally {
    cleanup();
    mounts.restore();
  }
});

test("switching a dark page under Lava to light and back keeps the one mount, its live palette following each theme, and never shows the fallback", async () => {
  const mounts = fakeMounts();
  try {
    stored("dark", "lava");
    await mount();
    const livePalette = () => lava.PALETTES[mounts.configs.lava![0]!.palette as keyof typeof lava.PALETTES];
    const seen: unknown[][] = [[livePalette(), hasFallback()]];
    act(() => chooseTheme("Dawn"));
    seen.push([livePalette(), hasFallback()]);
    act(() => chooseTheme("Night"));
    seen.push([livePalette(), hasFallback()]);
    const colours = (theme: typeof LIGHT) => [theme.ground, theme.back, theme.front];
    expect(seen).toEqual([[colours(DARK), false], [colours(LIGHT), false], [colours(DARK), false]]);
    expect(mounts.given.lava).toHaveLength(1);
  } finally {
    cleanup();
    mounts.restore();
  }
});

test("a light theme whose effect mount throws shows the fallback gradient", async () => {
  const mounts = fakeMounts({ throwing: true });
  try {
    stored("light", "haze");
    await mount();
    expect([hasFallback(), band().querySelector("canvas")]).toEqual([true, null]);
  } finally {
    cleanup();
    mounts.restore();
  }
});

test("a light theme applies the picked effect's Fade 40 and Opacity 50 to the band", async () => {
  const mounts = fakeMounts();
  try {
    stored("light", "caustics", { fade: 40, opacity: 50 });
    await mount();
    expect([band().style.getPropertyValue("--haze-fade"), band().style.opacity]).toEqual(["40%", "0.5"]);
  } finally {
    cleanup();
    mounts.restore();
  }
});

/** Animation frames that run only when `runFrames` is called, and a ResizeObserver whose callbacks run only when `resizeAll` is. */
function heldFrames() {
  const frames: FrameRequestCallback[] = [];
  const observers = new Set<() => void>();
  const savedObserver = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class {
    #fire: () => void;
    constructor(callback: ResizeObserverCallback) {
      this.#fire = () => callback([], this as unknown as ResizeObserver);
    }
    observe() {
      observers.add(this.#fire);
    }
    unobserve() {
      observers.delete(this.#fire);
    }
    disconnect() {
      observers.delete(this.#fire);
    }
  } as unknown as typeof ResizeObserver;
  const raf = spyOn(window, "requestAnimationFrame").mockImplementation((callback) => frames.push(callback));
  return {
    runFrames: () => frames.splice(0).forEach((callback) => callback(performance.now() + 16)),
    resizeAll: () => observers.forEach((fire) => fire()),
    restore: () => {
      globalThis.ResizeObserver = savedObserver;
      raf.mockRestore();
    },
  };
}

function sizeTo(canvas: HTMLCanvasElement, width: number, height: number) {
  Object.defineProperty(canvas, "clientWidth", { configurable: true, value: width });
  Object.defineProperty(canvas, "clientHeight", { configurable: true, value: height });
}

test("a moving haze whose canvas changes size draws at the new size in the resize callback, before the next animation frame", () => {
  const held = heldFrames();
  const container = document.createElement("div");
  const drawing = haze.mount(container, { motion: true });
  try {
    const canvas = container.querySelector("canvas")!;
    const before = canvasDraws.length;

    sizeTo(canvas, 400, 188);
    held.resizeAll();

    expect([canvas.width, canvas.height]).toEqual([400, 188]);
    expect(canvasDraws.slice(before)).toEqual([[400, 188]]);
  } finally {
    drawing.destroy();
    held.restore();
  }
});

test("a still haze redraws its static frame once at a new size and draws nothing for an unchanged size", () => {
  const held = heldFrames();
  const container = document.createElement("div");
  const drawing = haze.mount(container, { motion: false });
  try {
    const canvas = container.querySelector("canvas")!;
    held.runFrames();
    const settled = canvasDraws.length;

    sizeTo(canvas, 400, 188);
    held.resizeAll();
    expect(canvasDraws.slice(settled)).toEqual([[400, 188]]);

    held.runFrames();
    held.resizeAll();
    held.runFrames();
    expect(canvasDraws.slice(settled)).toEqual([[400, 188]]);
  } finally {
    drawing.destroy();
    held.restore();
  }
});
