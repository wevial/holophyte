import { describe, expect, test } from "bun:test";

import { declarations, resolveToken } from "../../tests/cssTokens.ts";
import { windowOptions } from "../window.ts";

const themeCss = await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text();
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
  test("on macOS hides the title bar, centres the traffic lights on the 52px title row and paints the Night canvas first", () => {
    const options = windowOptions("darwin");
    expect(options).toMatchObject(shared);
    expect(options.titleBarStyle).toBe("hiddenInset");
    expect(options.trafficLightPosition).toEqual({ x: 14, y: 19 });
    expect(options.backgroundColor).toBe(darkCanvas);
  });

  test("elsewhere keeps the native title bar and default background", () => {
    const options = windowOptions("linux");
    expect(options).toEqual(shared);
    expect(options.trafficLightPosition).toBeUndefined();
  });
});
