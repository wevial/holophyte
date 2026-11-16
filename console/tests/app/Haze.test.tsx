import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { THEME_KEY } from "../../src/lib/theme";
import type { Status } from "../../src/lib/types";
import { fakeDeps, NO_ATTENTION, settle, stubFetch } from "../harness";

const ORIGIN = "http://writer-host:7710";
const status = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;

/** A WebGL2 context that grants float targets, compiles every shader and links every program. */
const grantingGl = new Proxy({}, {
  get: (_, name) => {
    if (typeof name === "string" && /^[A-Z_0-9]+$/.test(name)) return name;
    if (name === "getProgramParameter") return (_program: unknown, pname: string) => (pname === "ACTIVE_UNIFORMS" ? 0 : true);
    if (name === "getShaderParameter") return () => true;
    if (name === "getExtension") return (extension: string) => (extension === "EXT_color_buffer_float" ? {} : null);
    return () => ({});
  },
});

let webgl2Asks = 0;

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  webgl2Asks = 0;
  spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(((kind: string) => {
    if (kind !== "webgl2") return null;
    webgl2Asks += 1;
    return grantingGl;
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
