import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { renderIcon, renderTrayIcons } from "../icon.ts";

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const STATE_COLOUR = { warn: "#F0B13A", bad: "#FF5F57" } as const;

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

describe("state glyphs", () => {
  for (const [variant, colour] of Object.entries(STATE_COLOUR)) {
    test(`menubar-${variant}.svg is the leaf silhouette in its state colour: no stroke, no vein cut-outs`, () => {
      const svg = readFileSync(path.resolve(import.meta.dirname, `../../../assets/menubar-${variant}.svg`), "utf8");
      expect(svg).not.toContain("stroke");
      const leaf = svg.match(/<path[^>]*>/)?.[0] ?? "";
      expect(leaf).toContain(`fill="${colour}"`);
      // The two lobes are two subpaths; the vein cut-outs the glyph used to
      // draw would each add another M command.
      expect(leaf.match(/d="([^"]*)"/)?.[1].match(/M/g)).toHaveLength(2);
      // The dot keeps the state colour over a bar-neutral ring so it
      // separates from the leaf on either bar.
      expect(svg).toContain('fill="#8E8E93"');
      expect(svg.match(new RegExp(`fill="${colour}"`, "g"))).toHaveLength(2);
      expect(Array.from(renderIcon(svg, 18).subarray(0, 8))).toEqual(PNG_SIGNATURE);
    });
  }
});

describe("renderTrayIcons", () => {
  test("writes the warn and bad PNGs at 1x and 2x and no -dark files", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "tray-icons-"));
    const written = renderTrayIcons(dir);
    const expected = ["menubar-bad@1x.png", "menubar-bad@2x.png", "menubar-warn@1x.png", "menubar-warn@2x.png"];
    expect(written.sort()).toEqual(expected);
    expect(readdirSync(dir).sort()).toEqual(expected);
    for (const file of expected) {
      expect(Array.from(readFileSync(path.join(dir, file)).subarray(0, 8))).toEqual(PNG_SIGNATURE);
    }
  });
});
