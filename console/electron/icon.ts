/**
 * The app icon, rendered from the repository's own SVG so no image tool has
 * to exist on the machine, and the tray's warn and bad variants, rendered
 * from the drawer's SVGs at the template PNG's 1x and 2x sizes since
 * Electron loads no SVG into a tray. `renderIcon` is pure; the CLI entry
 * writes the PNGs into the build output directory for `electron-builder`
 * and `main.ts` to pick up.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { Resvg } from "@resvg/resvg-js";

export const ICON_SIZE = 1024;
/** The template PNG is 18 px at 1x; the variants match it. */
export const TRAY_SIZE = 18;
export const TRAY_VARIANTS = ["warn", "bad"] as const;

/**
 * The warn and bad SVGs draw the glyph white with a dark stroke for a light
 * menu bar. They are colour images -- the state dot is the point -- so
 * macOS cannot recolour them the way it does the template glyph, and on a
 * dark menu bar the stroke reads as a black outline. This is the same
 * glyph for a dark bar: stroke light, fill dark, the dot untouched. Only
 * the first path is the glyph; the dot is the path after it.
 */
export function darkGlyph(svgText: string): string {
  const end = svgText.indexOf("/>");
  if (end < 0) return svgText;
  const glyph = svgText.slice(0, end).replace('fill="#FFFFFF"', 'fill="#1C1C1E"').replace('stroke="#1C1C1E"', 'stroke="#F5F5F7"');
  return glyph + svgText.slice(end);
}

export function renderIcon(svgText: string, size: number = ICON_SIZE): Uint8Array {
  const resvg = new Resvg(svgText, { fitTo: { mode: "width", value: size } });
  return resvg.render().asPng();
}

if (import.meta.main) {
  const here = import.meta.dirname;
  const svgPath = path.resolve(here, "..", "..", "assets", "logo.svg");
  const outPath = path.join(here, "dist", "icon.png");
  mkdirSync(path.dirname(outPath), { recursive: true });
  writeFileSync(outPath, renderIcon(readFileSync(svgPath, "utf8")));
  console.log(`wrote ${path.relative(process.cwd(), outPath)} (${ICON_SIZE}x${ICON_SIZE})`);
  for (const variant of TRAY_VARIANTS) {
    const svg = readFileSync(path.resolve(here, "..", "..", "assets", `menubar-${variant}.svg`), "utf8");
    for (const [suffix, text] of [["", svg], ["-dark", darkGlyph(svg)]] as const) {
      for (const scale of [1, 2]) {
        const file = path.join(here, "dist", `menubar-${variant}${suffix}@${scale}x.png`);
        writeFileSync(file, renderIcon(text, TRAY_SIZE * scale));
        console.log(`wrote ${path.relative(process.cwd(), file)}`);
      }
    }
  }
}
