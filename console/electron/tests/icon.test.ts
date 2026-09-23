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

describe("quiet template", () => {
  test("menubar-template.svg is the black leaf with its veins cut out by a mask, and its PNGs are 20 and 40 px tall", () => {
    const svg = readFileSync(path.resolve(import.meta.dirname, "../../../assets/menubar-template.svg"), "utf8");
    expect(svg).toContain('viewBox="36 24 248 182"');
    expect(svg).not.toContain("stroke");
    expect(svg).not.toContain("fill-rule");
    const paths = svg.match(/<path[^>]*>/g) ?? [];
    expect(paths).toHaveLength(2);
    const [veins = "", leaf] = paths;
    const mask = svg.match(/<mask id="([^"]*)">(.*?)<\/mask>/);
    expect(mask).toBeTruthy();
    const [, maskId, maskBody] = mask ?? [];
    expect(maskBody).toContain('<rect x="0" y="0" width="320" height="240" fill="#FFFFFF"/>');
    expect(veins).toContain('fill="#000"');
    expect(maskBody).toContain(veins);
    expect(leaf).toContain('fill="#000"');
    expect(leaf).toContain(`mask="url(#${maskId})"`);
    const leafD = leaf.match(/d="([^"]*)"/)?.[1] ?? "";
    const veinD = veins.match(/d="([^"]*)"/)?.[1] ?? "";
    expect(leafD.match(/M/g)).toHaveLength(2);
    expect(veinD.match(/M/g)).toHaveLength(8);
    // The warn glyph carries the same ten subpaths, so joining the
    // template's lobes and veins at the split reproduces the leaf the
    // state glyphs already draw.
    const warn = readFileSync(path.resolve(import.meta.dirname, "../../../assets/menubar-warn.svg"), "utf8");
    const [warnVeins, warnLeaf] = warn.match(/<path[^>]*>/g) ?? [];
    const warnD = [warnLeaf, warnVeins].map((p) => p?.match(/d="([^"]*)"/)?.[1] ?? "").join(" ");
    expect(`${leafD} ${veinD}`).toBe(warnD);
    for (const [file, height] of [["menubar-template@1x.png", 20], ["menubar-template@2x.png", 40]] as const) {
      const png = readFileSync(path.resolve(import.meta.dirname, `../../../assets/${file}`));
      expect(Array.from(png.subarray(0, 8))).toEqual(PNG_SIGNATURE);
      const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
      expect(new TextDecoder().decode(png.subarray(12, 16))).toBe("IHDR");
      expect(view.getUint32(20)).toBe(height);
    }
  });
});

describe("state glyphs", () => {
  const template = readFileSync(path.resolve(import.meta.dirname, "../../../assets/menubar-template.svg"), "utf8");
  // The template's path data is ten subpaths split on "Z ": the two lobes
  // first, then the eight veins. The glyphs draw the lobes filled and cut
  // the veins back out through the mask, so the leaf and vein `d`s joined
  // at that split reproduce the template `d` subpath for subpath.
  const [templateVeins, templateLeaf] = template.match(/<path[^>]*>/g) ?? [];
  const templateD = [templateLeaf, templateVeins].map((p) => p?.match(/d="([^"]*)"/)?.[1] ?? "").join(" ");

  for (const [variant, colour] of Object.entries(STATE_COLOUR)) {
    test(`menubar-${variant}.svg is the white leaf with veins cut out by a mask and the state dot beside it`, () => {
      const svg = readFileSync(path.resolve(import.meta.dirname, `../../../assets/menubar-${variant}.svg`), "utf8");
      expect(svg).toContain('viewBox="36 24 248 182"');
      expect(svg).not.toContain("stroke");
      expect(svg).not.toContain("fill-rule");
      const paths = svg.match(/<path[^>]*>/g) ?? [];
      expect(paths).toHaveLength(3);
      const [veins = "", leaf, disc] = paths;
      const mask = svg.match(/<mask id="([^"]*)">(.*?)<\/mask>/);
      expect(mask).toBeTruthy();
      const [, maskId, maskBody] = mask ?? [];
      // The mask is a white rect covering the viewBox minus the veins in
      // black, so every vein subpath stays cut even where it overlaps the
      // midrib.
      expect(maskBody).toContain('<rect x="0" y="0" width="320" height="240" fill="#FFFFFF"/>');
      expect(veins).toContain('fill="#000"');
      expect(maskBody).toContain(veins);
      expect(leaf).toContain('fill="#FFFFFF"');
      expect(leaf).toContain(`mask="url(#${maskId})"`);
      const leafD = leaf.match(/d="([^"]*)"/)?.[1] ?? "";
      const veinD = veins.match(/d="([^"]*)"/)?.[1] ?? "";
      expect(leafD.match(/M/g)).toHaveLength(2);
      expect(veinD.match(/M/g)).toHaveLength(8);
      expect(`${leafD} ${veinD}`).toBe(templateD);
      // The last path is the disc in the state colour; the bar-neutral ring
      // is gone.
      expect(disc).toContain(`fill="${colour}"`);
      expect(svg).not.toContain('fill="#8E8E93"');
      expect(svg.match(new RegExp(`fill="${colour}"`, "g"))).toHaveLength(1);
      expect(Array.from(renderIcon(svg, 18).subarray(0, 8))).toEqual(PNG_SIGNATURE);
    });
  }
});

describe("renderTrayIcons", () => {
  test("writes the warn and bad PNGs at 1x and 2x and no -dark files", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "tray-icons-"));
    const written = renderTrayIcons(dir);
    const expected = [
      ["menubar-bad@1x.png", 20],
      ["menubar-bad@2x.png", 40],
      ["menubar-warn@1x.png", 20],
      ["menubar-warn@2x.png", 40],
    ] as const;
    const names = expected.map(([name]) => name);
    expect(written.sort()).toEqual(names);
    expect(readdirSync(dir).sort()).toEqual(names);
    for (const [file, height] of expected) {
      const png = readFileSync(path.join(dir, file));
      expect(Array.from(png.subarray(0, 8))).toEqual(PNG_SIGNATURE);
      const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
      expect(new TextDecoder().decode(png.subarray(12, 16))).toBe("IHDR");
      expect(view.getUint32(20)).toBe(height);
    }
  });
});
