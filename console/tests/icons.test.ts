import { expect, test } from "bun:test";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { inflateSync } from "node:zlib";
import { buildConsole } from "../build";

const publicDir = new URL("../public/", import.meta.url);
const bytes = async (name: string) => new Uint8Array(await Bun.file(new URL(name, publicDir)).arrayBuffer());

/** The paper colour as `theme.css` declares it on `:root`, so the icons'
 *  ground is checked against the live token rather than a copied literal. */
const css = await Bun.file(new URL("../src/theme.css", import.meta.url)).text();
const paper = /:root\s*\{[^}]*--paper:\s*(#[0-9a-f]{6})/i.exec(css)![1]!;

/** IHDR fields of a PNG plus its first scanline after unfiltering, read
 *  without an image library: width, height, colour type (2 = RGB without
 *  alpha, 6 = RGBA) and, for RGBA, the alpha of every pixel in that row. */
function png(data: Uint8Array) {
  const view = new DataView(data.buffer, data.byteOffset);
  expect(Array.from(data.slice(0, 8))).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const width = view.getUint32(16);
  const height = view.getUint32(20);
  const colourType = data[25]!;
  const idat: Uint8Array[] = [];
  for (let at = 8; at < data.length; ) {
    const length = view.getUint32(at);
    const type = String.fromCharCode(...data.slice(at + 4, at + 8));
    if (type === "IDAT") idat.push(data.slice(at + 8, at + 8 + length));
    at += 12 + length;
  }
  const raw = inflateSync(Buffer.concat(idat));
  const channels = colourType === 6 ? 4 : 3;
  // First scanline: the prior row is all zero, so Up is None, Average
  // halves the left neighbour, and Paeth degrades to Sub.
  const filter = raw[0]!;
  const row = new Uint8Array(raw.subarray(1, 1 + width * channels));
  for (let i = 0; i < row.length; i++) {
    const left = i >= channels ? row[i - channels]! : 0;
    if (filter === 1 || filter === 4) row[i] = (row[i]! + left) & 0xff;
    else if (filter === 3) row[i] = (row[i]! + (left >> 1)) & 0xff;
    else if (filter !== 0) throw new Error(`unexpected PNG filter ${filter}`);
  }
  const firstRowAlpha = colourType === 6 ? Array.from({ length: width }, (_, x) => row[x * 4 + 3]!) : [];
  return { width, height, colourType, firstRowAlpha };
}

test.each([
  ["apple-touch-icon.png", 180, 180],
  ["icon-192.png", 192, 192],
  ["icon-512.png", 512, 512],
])("%s is a %ix%i PNG with an opaque ground", async (name, size, _height) => {
  const image = png(await bytes(name));
  expect([image.width, image.height]).toEqual([size, size]);
  if (image.colourType === 6) {
    expect(image.firstRowAlpha.every((alpha) => alpha === 255)).toBe(true);
  } else {
    expect(image.colourType).toBe(2);
  }
});

test("the manifest names both PNGs with sizes, standalone display and the paper colours", async () => {
  const manifest = JSON.parse(await Bun.file(new URL("manifest.webmanifest", publicDir)).text());
  expect(manifest.icons.map((icon: { src: string; sizes: string }) => [icon.src, icon.sizes])).toEqual([
    ["/icon-192.png", "192x192"],
    ["/icon-512.png", "512x512"],
  ]);
  expect(manifest.display).toBe("standalone");
  expect(manifest.background_color).toBe(paper);
  expect(manifest.theme_color).toBe(paper);
  const svg = await Bun.file(new URL("favicon.svg", publicDir)).text();
  expect(svg).toContain('viewBox="28 -19 263 263"');
  expect(svg).toMatch(/<rect class="tile" x="28" y="-19" width="263" height="263"/);
  expect(svg).toContain(`fill: ${paper}`);
});

test("the build carries public/ into dist and index.html links the icons and manifest", async () => {
  const outdir = `${await mkdtemp(`${tmpdir()}/console-dist-`)}/`;
  try {
    await buildConsole(outdir);
    const listed = await readdir(outdir);
    for (const name of ["favicon.svg", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "manifest.webmanifest"]) {
      expect(listed).toContain(name);
      expect(await bytes(name)).toEqual(new Uint8Array(await Bun.file(outdir + name).arrayBuffer()));
    }
    const html = await Bun.file(`${outdir}index.html`).text();
    expect(html).toMatch(/<link rel="icon" href="\/favicon\.svg"/);
    expect(html).toMatch(/<link rel="apple-touch-icon" href="\/apple-touch-icon\.png"/);
    expect(html).toMatch(/<link rel="manifest" href="\/manifest\.webmanifest"/);
    expect(html).toMatch(/<meta name="theme-color" media="\(prefers-color-scheme: light\)" content="#f4f1ea"/);
    expect(html).toMatch(/<meta name="theme-color" media="\(prefers-color-scheme: dark\)" content="#141210"/);
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
}, 30_000);
