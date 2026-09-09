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
    for (const scale of [1, 2]) {
      const file = path.join(here, "dist", `menubar-${variant}@${scale}x.png`);
      writeFileSync(file, renderIcon(svg, TRAY_SIZE * scale));
      console.log(`wrote ${path.relative(process.cwd(), file)}`);
    }
  }
}
