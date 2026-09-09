/**
 * The app icon, rendered from the repository's own SVG so no image tool has
 * to exist on the machine. `renderIcon` is pure; the CLI entry writes the
 * PNG into the build output directory for `electron-builder` to pick up.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { Resvg } from "@resvg/resvg-js";

export const ICON_SIZE = 1024;

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
}
