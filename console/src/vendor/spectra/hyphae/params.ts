// Vendored from wevial/spectra@4d4c754 backgrounds/hyphae/src/params.ts (MIT, see ../LICENSE).
// Pure parameter math for the hyphae background, kept out of the GL code so
// it can be tested.

/**
 * Four colors: ground, haze (faint exploration), hyphae, hot feeding tips.
 * `spectral` hues the hyphae by place; `prism` keeps them silver and splits
 * their edges into a spectrum.
 */
export const PALETTES = {
  foxfire: ['#030605', '#0f3a30', '#7ff2c4', '#eafff4'],
  ember: ['#070302', '#4a1606', '#ff9a52', '#fff0d8'],
  spore: ['#05030a', '#2c1452', '#c3a0ff', '#ffe0f6'],
  frost: ['#030407', '#16243c', '#b2c9ff', '#ffffff'],
  spectral: ['#040406', '#20202a', '#ffffff', '#ffffff'],
  prism: ['#040406', '#262830', '#d4d8e2', '#ffffff'],
} satisfies Record<string, [string, string, string, string]>;

export type PaletteName = keyof typeof PALETTES;

export const MAX_PATCHES = 8;
export const MAX_TAPS = 8;

/** sRGB hex to linear rgb. */
export function hexToLinear(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  const lin = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return [lin((n >> 16 & 255) / 255), lin((n >> 8 & 255) / 255), lin((n & 255) / 255)];
}

/** Deterministic 0..1 hash of a number. */
function hash(n: number) {
  const x = Math.sin(n * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
}

/**
 * The invisible food: `count` nutrient patches, packed as vec4s (x, y in uv,
 * radius in fractions of the height, strength 0..1). Each patch lives a slow
 * cycle (`period` seconds): it swells in
 * from nothing, peaks and fades out, then is reborn somewhere else. Phases
 * are staggered so some food is always around. While alive a patch drifts.
 */
export function patches(time: number, count: number, seed: number, period = 60) {
  const n = Math.max(1, Math.min(MAX_PATCHES, Math.round(count)));
  const out = new Float32Array(MAX_PATCHES * 4);
  for (let i = 0; i < n; i++) {
    const s = seed * 17.3 + i * 7.1;
    // One shared cycle with evenly staggered phases: sin² summed over them
    // is constant, so the total food only varies with each life's peak.
    const u = time / period + i / n;
    const cycle = Math.floor(u);
    const c = s + cycle * 31.7;
    const life = Math.sin(Math.PI * (u - cycle)) ** 2 * (0.65 + 0.35 * hash(c + 1.9));
    const drift = time * 0.011 * (0.6 + 0.8 * hash(c + 4.4));
    const x = hash(c + 2.3) + 0.05 * Math.sin(drift + 6.28 * hash(c + 5.1));
    const y = hash(c + 3.7) + 0.05 * Math.cos(drift * 0.83 + 6.28 * hash(c + 6.2));
    out.set([x - Math.floor(x), y - Math.floor(y), 0.14 + 0.12 * hash(c + 8.8), life], i * 4);
  }
  return { count: n, data: out };
}

/** Spore clusters the colony first blooms from: vec4s of (x, y in uv, radius in height fractions, 0). */
export function seedClusters(rand: () => number, count = 4) {
  const out = new Float32Array(4 * 4);
  const n = Math.max(1, Math.min(4, count));
  for (let i = 0; i < n; i++) out.set([0.15 + 0.7 * rand(), 0.15 + 0.7 * rand(), 0.03 + 0.03 * rand(), 0], i * 4);
  return { count: n, data: out };
}

/**
 * Linear-sampling taps for a Gaussian of `sigma` texels: the center, then
 * pairs of texels read with one bilinear fetch between them. Weights cover
 * both sides and sum to 1.
 */
export function gaussianTaps(sigma: number) {
  const radius = Math.min(Math.max(1, Math.ceil(sigma * 3)), (MAX_TAPS - 1) * 2);
  const w = Array.from({ length: radius + 1 }, (_, k) => Math.exp((-k * k) / (2 * sigma * sigma)));
  const total = w[0] + 2 * w.slice(1).reduce((a, b) => a + b, 0);
  const offsets = [0];
  const weights = [w[0] / total];
  for (let k = 1; k <= radius; k += 2) {
    const a = w[k], b = k + 1 <= radius ? w[k + 1] : 0;
    offsets.push((k * a + (k + 1) * b) / (a + b));
    weights.push((a + b) / total);
  }
  const pad = (v: number[]) => new Float32Array(v.concat(Array(MAX_TAPS - v.length).fill(0)));
  return { count: offsets.length, offsets: pad(offsets), weights: pad(weights) };
}
