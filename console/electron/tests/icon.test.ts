import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import path from "node:path";

import { renderIcon } from "../icon.ts";

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
