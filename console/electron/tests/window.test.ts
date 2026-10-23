import { describe, expect, test } from "bun:test";

import { declarations, resolveToken } from "../../tests/cssTokens.ts";
import { windowOptions } from "../window.ts";

const themeCss = await Bun.file(new URL("../../src/next/theme.css", import.meta.url)).text();
const darkCanvas = resolveToken(declarations(themeCss), [':root[data-theme="dark"]'], "--canvas");

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
    expect(options.backgroundColor).toBe("#0d0a15");
    expect(options.backgroundColor).toBe(darkCanvas);
  });

  test("elsewhere keeps the native title bar and default background", () => {
    const options = windowOptions("linux");
    expect(options).toEqual(shared);
  });
});
