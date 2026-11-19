// Vendored from wevial/spectra@4d4c754 backgrounds/ribbons/src/params.ts (MIT, see ../LICENSE).
// Pure helpers, no GL: palettes, per-ribbon wave parameters, Gaussian taps.

/**
 * Ribbon colors, sRGB, one per ribbon in order (a palette is cycled past its
 * length). Ordered so neighbors contrast: with any count the ribbons that
 * cross differ in color.
 */
export const PALETTES = {
  /** Warm red and orange against cool blue and cyan. */
  flare: ['#ff3a1c', '#1f6bff', '#ff8a1e', '#14c8ff', '#ff5a78', '#4d4dff'],
  /** Hues around the wheel. */
  spectrum: ['#ff2a2a', '#ffb21c', '#4dff6a', '#18c8ff', '#3a4dff', '#c03aff'],
  /** Near-white, faintly warm and cool, with stronger spectral edges. */
  silver: ['#c4c8d0', '#d0cac2', '#bcc4d0', '#ccc8cc', '#c2c6cc', '#cac4be'],
  /** Teal, green and violet. */
  lagoon: ['#12e0c0', '#7a3dff', '#3dff8a', '#2a8cff', '#c04dff', '#1affe0'],
  /** Pink, magenta and peach. */
  orchid: ['#ff3d9a', '#9a3dff', '#ff9a6a', '#ff4dff', '#6a5aff', '#ffb0c8'],
} satisfies Record<string, string[]>;

export type PaletteName = keyof typeof PALETTES;

/** Spectral edge strength per palette: silver leans on its fringes for color. */
export const FRINGE: Partial<Record<PaletteName, number>> = { silver: 1.8 };

export const MAX_RIBBONS = 6;

export function hexToLinear(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  const lin = (c: number) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  return [lin((n >> 16 & 255) / 255), lin((n >> 8 & 255) / 255), lin((n & 255) / 255)];
}

export function hash(n: number) {
  const s = Math.sin(n * 12.9898 + 78.233) * 43758.5453;
  return s - Math.floor(s);
}

export type Placement = 'center' | 'full' | 'fill';

export interface RibbonOptions {
  placement: Placement;
  count: number;
  amplitude: number;
  thickness: number;
  spread: number;
  /** 0..1: how much the back ribbons shrink (the shader also softens and dims them). */
  depth: number;
  seed: number;
}

/**
 * Per-ribbon uniforms at time `t`, packed for the shader:
 * - wave: two sines along the ribbon (frequency, phase, frequency, phase),
 *   x running -1..1 from one pinch point to the other;
 * - shape: amplitude, vertical offset, half-thickness at the middle, and the
 *   phase of a slow swell that runs along the ribbon;
 * - extra: sheen frequency and phase, depth (0 front, 1 back), and where a
 *   traveling glint is along x (past ±1.4 when none is running).
 *
 * Every ribbon drifts at its own incommensurate speeds, so the braid never
 * repeats; amplitude and thickness breathe on periods of 10–30s.
 */
export function ribbonParams(o: RibbonOptions, t: number) {
  const count = Math.max(1, Math.min(MAX_RIBBONS, Math.round(o.count)));
  const wave = new Float32Array(MAX_RIBBONS * 4);
  const shape = new Float32Array(MAX_RIBBONS * 4);
  const extra = new Float32Array(MAX_RIBBONS * 4);
  // A slow common breath: everything swells and settles together, slightly.
  const breath = 1 + 0.08 * Math.sin(t * 0.55);
  for (let i = 0; i < count; i++) {
    const r = (k: number) => hash(o.seed * 13.7 + i * 7.1 + k);
    // About one wavelength across: the S-curve of a ribbon, not a zigzag.
    const f1 = 2.6 + 1.6 * r(1);
    const f2 = 4.6 + 2.2 * r(2);
    const s1 = (0.22 + 0.16 * r(3)) * (r(4) < 0.5 ? -1 : 1);
    const s2 = (0.13 + 0.12 * r(5)) * (r(6) < 0.5 ? -1 : 1);
    wave.set([f1, r(7) * Math.PI * 2 + t * s1, f2, r(8) * Math.PI * 2 + t * s2], i * 4);

    // Depths spread evenly front to back, in a seeded order.
    const z = (i * 0.618 + hash(o.seed * 3.1 + 0.5)) % 1;
    const far = 1 - 0.25 * z * o.depth;
    const amp = far * o.amplitude * (0.62 + 0.38 * Math.sin(t * (0.21 + 0.1 * r(9)) + r(10) * 6.283)) * breath;
    // Spread: ribbons fan out from a common center line, offsets wandering a little.
    const lane = count > 1 ? i / (count - 1) - 0.5 : 0;
    // Filling, each ribbon has its own frame (see ribbonFrames), so they
    // don't fan out from a shared line.
    const fan = o.placement === 'fill' ? 0 : lane * 0.5;
    const offset = o.spread * (fan + 0.08 * Math.sin(t * (0.17 + 0.08 * r(11)) + r(12) * 6.283));
    const thick = far * o.thickness * (0.7 + 0.5 * r(13)) * (0.9 + 0.1 * Math.sin(t * (0.3 + 0.1 * r(14)) + i)) * breath;
    shape.set([amp, offset, thick, r(15) * 6.283 + t * (0.25 + 0.15 * r(16))], i * 4);

    // The sheen's hue rolls about a half to one full cycle along the ribbon.
    const sheenSpeed = (0.12 + 0.13 * r(17)) * (r(18) < 0.5 ? -1 : 1);
    // A glint every 14–26s, taking ~3.5s to cross.
    const period = 14 + 12 * r(19);
    const since = ((t / period + r(20)) % 1) * period;
    extra.set([1.8 + 1.6 * r(21), r(22) * 6.283 + t * sheenSpeed, z, -1.4 + (since / 3.5) * 2.8], i * 4);
  }
  return { count, wave, shape, extra };
}

/**
 * Where each ribbon sits, packed vec4s: center x, y and angle in box units
 * (x spans -box.x/2..box.x/2), and a scale on its length. Centered and
 * full-bleed every ribbon shares one frame, so they pinch at the same two
 * points. `fill` spreads the ribbons over the height, each with its own
 * length and a tilt that leans the outer ones toward the middle, so they
 * cross; the frames wander over minutes.
 * Some pinch points land just inside the sides, some past them.
 */
export function ribbonFrames(o: RibbonOptions, box: readonly [number, number], t: number) {
  const count = Math.max(1, Math.min(MAX_RIBBONS, Math.round(o.count)));
  const frames = new Float32Array(MAX_RIBBONS * 4);
  for (let i = 0; i < MAX_RIBBONS; i++) frames.set([0, 0, 0, 1], i * 4);
  if (o.placement !== 'fill') return frames;
  const span = box[1] * Math.min(0.8, 0.3 + o.spread);
  for (let i = 0; i < count; i++) {
    const r = (k: number) => hash(o.seed * 5.3 + i * 3.7 + k + 40);
    // Lanes in palette order, which alternates warm and cool.
    const lane = count > 1 ? i / (count - 1) - 0.5 : 0;
    const y = lane * span + 0.06 * box[1] * Math.sin(t * (0.05 + 0.03 * r(1)) + r(2) * 6.283);
    const x = 0.12 * box[0] * Math.sin(t * (0.03 + 0.02 * r(3)) + r(4) * 6.283);
    const angle = -lane * 1.1 * (box[1] / box[0]) + (r(5) - 0.5) * 0.18 + 0.06 * Math.sin(t * (0.04 + 0.02 * r(6)) + r(7) * 6.283);
    // Some reach past the sides, some pinch just inside them.
    const scale = (box[0] / 2) * (0.9 + 0.45 * r(8)) / Math.cos(angle);
    frames.set([x, y, angle, scale], i * 4);
  }
  return frames;
}

/** Linear colors for every ribbon slot from a palette, cycling it, packed vec3s. */
export function ribbonColors(palette: readonly string[]) {
  const out = new Float32Array(MAX_RIBBONS * 3);
  for (let i = 0; i < MAX_RIBBONS; i++) out.set(hexToLinear(palette[i % palette.length]), i * 3);
  return out;
}

export const MAX_TAPS = 8;

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
