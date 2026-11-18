// Vendored from wevial/spectra@d34e0f8 cursors/film/src/spectrum.ts (MIT, see ../LICENSE).
// Wavelength to color. The CIE 1931 2° color matching functions, as Wyman,
// Sloan & Shirley's multi-lobe Gaussian fit, converted to linear sRGB.
// Pure spectral colors lie outside sRGB; see toGamut().

export const NM_MIN = 380;
export const NM_MAX = 780;

function lobe(nm: number, mu: number, below: number, above: number) {
  const t = (nm - mu) / (nm < mu ? below : above);
  return Math.exp(-0.5 * t * t);
}

/** CIE 1931 XYZ of one nanometer of monochromatic light. */
export function xyz(nm: number): [number, number, number] {
  return [
    1.056 * lobe(nm, 599.8, 37.9, 31.0) + 0.362 * lobe(nm, 442.0, 16.0, 26.7) - 0.065 * lobe(nm, 501.1, 20.4, 26.2),
    0.821 * lobe(nm, 568.8, 46.9, 40.5) + 0.286 * lobe(nm, 530.9, 16.3, 31.1),
    1.217 * lobe(nm, 437.0, 11.8, 36.0) + 0.681 * lobe(nm, 459.0, 26.0, 13.8),
  ];
}

type RGB = [number, number, number];

/** Linear sRGB of monochromatic light, out of gamut (some channels negative). */
function rawRgb(nm: number): RGB {
  const [x, y, z] = xyz(nm);
  return [
    3.2406 * x - 1.5372 * y - 0.4986 * z,
    -0.9689 * x + 1.8758 * y + 0.0415 * z,
    0.0557 * x - 0.204 * y + 1.057 * z,
  ];
}

/**
 * Mix partway toward the gray of the same luminance, then clip what's still
 * negative. All the way in gamut tints the reds pink; clipping alone leaves
 * dark gaps.
 */
function toGamut(rgb: RGB): RGB {
  const y = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
  const lo = Math.min(...rgb);
  const fit = lo < 0 ? Math.max(y, 0) / (Math.max(y, 0) - lo) : 1;
  const t = fit + (1 - fit) * 0.6;
  return rgb.map((c) => Math.max(y + (c - y) * t, 0)) as RGB;
}

/** Linear sRGB of monochromatic light, mapped into the gamut. */
export function wavelengthToRgb(nm: number): RGB {
  return toGamut(rawRgb(nm));
}

/**
 * RGBA table of `n` wavelengths spanning NM_MIN..NM_MAX, white-balanced so
 * the table averages to (1, 1, 1): a whole spectrum, smeared out, adds back
 * up to white light. Balanced before the gamut mapping, so the gray it adds
 * isn't tinted.
 */
export function spectrumTable(n = 256): Float32Array {
  const raw = Array.from({ length: n }, (_, i) => rawRgb(NM_MIN + ((NM_MAX - NM_MIN) * i) / (n - 1)));
  const mean = (rows: RGB[]) => [0, 1, 2].map((c) => rows.reduce((s, v) => s + v[c], 0) / n);
  const balance = mean(raw);
  const mapped = raw.map((v) => toGamut(v.map((c, k) => c / balance[k]) as RGB));
  const fix = mean(mapped);
  const out = new Float32Array(n * 4);
  mapped.forEach((v, i) => out.set([v[0] / fix[0], v[1] / fix[1], v[2] / fix[2], 1], i * 4));
  return out;
}
