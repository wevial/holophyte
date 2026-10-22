import { describe, expect, test } from "bun:test";

import { declarations } from "../../tests/cssTokens.ts";
import { windowOptions } from "../window.ts";

const themeCss = await Bun.file(new URL("../../src/next/theme.css", import.meta.url)).text();
const darkCanvas = declarations(themeCss).find(
  (decl) => decl.name === "--canvas" && decl.path.length === 1 && decl.path[0] === ':root[data-theme="dark"]',
)?.value;

const shared = {
  width: 1280,
  height: 860,
  minWidth: 1100,
  minHeight: 600,
  title: "Holophyte Console",
  webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
};

describe("windowOptions", () => {
  test("on macOS hides the title bar, places the traffic lights and paints the Night canvas first", () => {
    const options = windowOptions("darwin");
    expect(options).toMatchObject(shared);
    expect(options.titleBarStyle).toBe("hiddenInset");
    expect(options.trafficLightPosition).toEqual({ x: expect.any(Number), y: expect.any(Number) });
    expect(darkCanvas).toMatch(/^#[0-9a-f]{6}$/i);
    expect(options.backgroundColor).toBe(darkCanvas!);
  });

  test("elsewhere keeps the native title bar and default background", () => {
    const options = windowOptions("linux");
    expect(options).toEqual(shared);
  });
});
