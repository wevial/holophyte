/**
 * The app icon, rendered from the repository's own SVG so no image tool has
 * to exist on the machine: `assets/icon.svg` is the leaf on its own cream
 * tile, full-bleed, because macOS 26 drops an icon without a background onto
 * a grey tile, shrinks it and lays glass over it, which washed the leaf out; and the tray's warn and bad variants, rendered
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

export function renderIcon(svgText: string, size: number = ICON_SIZE): Uint8Array {
  const resvg = new Resvg(svgText, { fitTo: { mode: "width", value: size } });
  return resvg.render().asPng();
}

/**
 * The tray's warn and bad PNGs, one file per variant at 1x and 2x. They are
 * colour images -- the state dot is the point -- so macOS cannot recolour
 * them the way it does the template glyph, and they carry no dark strokes,
 * so one file reads on a light bar or a dark one. Returns the file names
 * written into `outDir`.
 */
export function renderTrayIcons(outDir: string): string[] {
  const written: string[] = [];
  mkdirSync(outDir, { recursive: true });
  for (const variant of TRAY_VARIANTS) {
    const svg = readFileSync(path.resolve(import.meta.dirname, "..", "..", "assets", `menubar-${variant}.svg`), "utf8");
    for (const scale of [1, 2]) {
      const file = `menubar-${variant}@${scale}x.png`;
      writeFileSync(path.join(outDir, file), renderIcon(svg, TRAY_SIZE * scale));
      written.push(file);
    }
  }
  return written;
}

if (import.meta.main) {
  const here = import.meta.dirname;
  const svgPath = path.resolve(here, "..", "..", "assets", "icon.svg");
  const outPath = path.join(here, "dist", "icon.png");
  mkdirSync(path.dirname(outPath), { recursive: true });
  writeFileSync(outPath, renderIcon(readFileSync(svgPath, "utf8")));
  console.log(`wrote ${path.relative(process.cwd(), outPath)} (${ICON_SIZE}x${ICON_SIZE})`);
  const distDir = path.join(here, "dist");
  for (const file of renderTrayIcons(distDir)) {
    console.log(`wrote ${path.relative(process.cwd(), path.join(distDir, file))}`);
  }
}
