import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import path from "node:path";

import { darkGlyph, renderIcon } from "../icon.ts";

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

describe("renderIcon", () => {
  test("renders the repository logo to a 1024x1024 PNG", () => {
    const svg = readFileSync(path.resolve(import.meta.dirname, "../../../assets/logo.svg"), "utf8");
    const png = renderIcon(svg, 1024);
    expect(Array.from(png.subarray(0, 8))).toEqual(PNG_SIGNATURE);
    // After the 8-byte signature: 4-byte length, "IHDR", then width and height.
    const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
    expect(new TextDecoder().decode(png.subarray(12, 16))).toBe("IHDR");
    expect(view.getUint32(16)).toBe(1024);
    expect(view.getUint32(20)).toBe(1024);
  });
});

describe("darkGlyph", () => {
  test("swaps the glyph's fill and stroke for a dark menu bar and leaves the state dot alone", () => {
    const svg = readFileSync(path.resolve(import.meta.dirname, "../../../assets/menubar-warn.svg"), "utf8");
    const dark = darkGlyph(svg);
    expect(dark).not.toBe(svg);
    const [glyph, dot] = dark.split("/>");
    expect(glyph).toContain('fill="#1C1C1E"');
    expect(glyph).toContain('stroke="#F5F5F7"');
    expect(glyph).not.toContain('stroke="#1C1C1E"');
    expect(dot).toContain('fill="#F0B13A"');
    // It still renders.
    expect(Array.from(renderIcon(dark, 18).subarray(0, 8))).toEqual(PNG_SIGNATURE);
  });
});
