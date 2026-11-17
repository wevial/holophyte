import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { THEME_KEY } from "../../src/lib/theme";
import { mount as mountHaze } from "../../src/vendor/spectra/haze";
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

test("a stored light theme paints the fallback glow with no canvas and never asks for WebGL2, though a context would be granted", async () => {
  localStorage.setItem(THEME_KEY, "light");
  await mount();
  expect(band().classList.contains("haze-fallback")).toBe(true);
  expect(band().querySelector("canvas")).toBeNull();
  expect(webgl2Asks).toBe(0);
});

test("a stored dark theme mounts the shader, asking for a WebGL2 context", async () => {
  localStorage.setItem(THEME_KEY, "dark");
  await mount();
  expect(webgl2Asks).toBe(1);
  expect(band().classList.contains("haze-fallback")).toBe(false);
  expect(band().querySelectorAll("canvas").length).toBe(1);
});

test("switching a dark page to light and back swaps the band to the fallback and back to one shader canvas", async () => {
  localStorage.setItem(THEME_KEY, "dark");
  await mount();
  expect(webgl2Asks).toBe(1);

  act(() => chooseTheme("Dawn"));
  expect(band().classList.contains("haze-fallback")).toBe(true);
  expect(document.querySelectorAll("canvas").length).toBe(0);
  expect(webgl2Asks).toBe(1);

  act(() => chooseTheme("Night"));
  expect(webgl2Asks).toBe(2);
  expect(band().classList.contains("haze-fallback")).toBe(false);
  expect(document.querySelectorAll("canvas").length).toBe(1);
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
  const haze = mountHaze(container, { motion: true });
  try {
    const canvas = container.querySelector("canvas")!;
    const before = canvasDraws.length;

    sizeTo(canvas, 400, 188);
    held.resizeAll();

    expect([canvas.width, canvas.height]).toEqual([400, 188]);
    expect(canvasDraws.slice(before)).toEqual([[400, 188]]);
  } finally {
    haze.destroy();
    held.restore();
  }
});

test("a still haze redraws its static frame once at a new size and draws nothing for an unchanged size", () => {
  const held = heldFrames();
  const container = document.createElement("div");
  const haze = mountHaze(container, { motion: false });
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
    haze.destroy();
    held.restore();
  }
});
