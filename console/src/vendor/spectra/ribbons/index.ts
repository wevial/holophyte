// Vendored from wevial/spectra@d34e0f8 backgrounds/ribbons/src/index.ts (MIT, see ../LICENSE), given a motion option.
// Ribbons background — translucent ribbons of colored light ripple across a
// black field. They pinch to a point at both ends, cross over each other,
// and add up toward white where they overlap, under a wide soft bloom.
// Ignores the pointer.

import { Program, VERTEX, makeBlit, createFBO } from '../core/gl';
import { createStage } from '../core/stage';
import { setGrain } from '../core/glsl';
import type { FBO } from '../core/gl';
import { SEED, BLUR, COMPOSITE } from './shaders';
import { PALETTES, FRINGE, ribbonParams, ribbonFrames, ribbonColors, gaussianTaps } from './params';
import type { PaletteName, Placement } from './params';

export { PALETTES } from './params';
export type { PaletteName, Placement } from './params';

export const DEFAULTS = {
  palette: 'silver' as PaletteName,
  /** Number of ribbons, 1–6. */
  count: 4,
  /** How far the ribbons swing up and down, in half-spans. */
  amplitude: 0.3,
  /** Half-thickness of a ribbon at its middle, in half-spans. */
  thickness: 0.08,
  /** How far apart the ribbons fan out vertically. */
  spread: 0.4,
  /** Drift speed. */
  speed: 1,
  /** Bloom strength. */
  glow: 1,
  /** Brightness of the ribbons themselves. */
  intensity: 1,
  /**
   * `center`: a band in the middle of the box. `full`: the band runs edge to
   * edge and swings taller. `fill`: each ribbon gets its own height, tilt
   * and pinch points past the sides, spread over the whole box.
   */
  placement: 'full' as Placement,
  /** Front-to-back depth: back ribbons are smaller, softer and dimmer. */
  depth: 0.6,
  /** Thin-film color shift, rolling along and across each ribbon. */
  sheen: 0.35,
  /** Brightness of the glints that now and then run along a ribbon. */
  glints: 0.6,
  /** While on, the ribbons swell in a slow rhythm, as if listening or speaking. Eases in and out. */
  pulsing: false,
  /** Film grain. */
  grain: 0.015,
  /** Seconds for the grain to morph into a new pattern; 0 freezes it. */
  grainPeriod: 1.5,
  /** Picks the ribbons' frequencies, phases and drift. */
  seed: 0,
  /** false freezes the drift and grain on one frame, as reduced motion does. */
  motion: true,
};

export type RibbonsConfig = typeof DEFAULTS;

export interface RibbonsHandle {
  /** Live config: mutate fields and the next frame picks them up. */
  config: RibbonsConfig;
  /** A surge: the ribbons swing wider, speed up and brighten, then settle over ~2s. Stacks. */
  pulse(strength?: number): void;
  destroy(): void;
}

const REVEAL_MS = 1200;
/** Chromatic fringe, as a fraction of the half-width. */
const FRINGE_BASE = 0.07;
/** Sheen strength per palette: silver gets its color from the film. */
const SHEEN: Partial<Record<PaletteName, number>> = { silver: 1.3 };
/** Seconds per swell while `pulsing`. */
const PULSE_PERIOD = 1.6;
/** Pulse energy decay, per second. */
const PULSE_DECAY = 1.6;
/** How hard overlaps burn to white. */
const WHITE = 0.6;
/** Halo blur, sigma in quarter-resolution texels (~10 CSS px). */
const HALO_SIGMA = 2.5;
/** Wide glow blur, sigma in eighth-resolution texels, applied twice (~45 CSS px). */
const WIDE_SIGMA = 4;

/**
 * Mount the effect into `container` (which should be positioned and sized).
 * Without WebGL2 + float targets the container gets the `ribbons-fallback`
 * class instead.
 */
export function mount(container: HTMLElement, opts: Partial<RibbonsConfig> = {}): RibbonsHandle {
  const config: RibbonsConfig = { ...DEFAULTS, ...opts };

  let resizeGlow = () => {};
  let drawn = '';
  const stage = createStage(container, {
    floatTargets: true,
    onResize: () => { resizeGlow(); drawn = ''; },
  });
  if (!stage) {
    container.classList.add('ribbons-fallback');
    return { config, pulse() {}, destroy() {} };
  }
  const { gl, canvas, reducedMotion } = stage;
  const blit = makeBlit(gl);
  const seed = new Program(gl, VERTEX, SEED);
  const blur = new Program(gl, VERTEX, BLUR);
  const composite = new Program(gl, VERTEX, COMPOSITE);
  const haloTaps = gaussianTaps(HALO_SIGMA);
  const wideTaps = gaussianTaps(WIDE_SIGMA);
  // One center tap: a plain bilinear copy.
  const copyTaps = { count: 1, offsets: new Float32Array(8), weights: new Float32Array([1, 0, 0, 0, 0, 0, 0, 0]) };

  // Bloom targets sized from CSS pixels, so the glow is the same width at any DPR.
  let halo: FBO | null = null;
  let haloScratch: FBO | null = null;
  let wide: FBO | null = null;
  let wideScratch: FBO | null = null;
  resizeGlow = () => {
    for (const f of [halo, haloScratch, wide, wideScratch]) f?.dispose();
    const target = (div: number) => createFBO(gl,
      Math.max(1, Math.round(canvas.clientWidth / div)), Math.max(1, Math.round(canvas.clientHeight / div)),
      gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
    halo = target(4);
    haloScratch = target(4);
    wide = target(8);
    wideScratch = target(8);
  };
  resizeGlow();

  let time = 0;
  let reveal = reducedMotion.matches || !config.motion ? 1 : 0;
  let grainClock = 0;
  let energy = 0;      // one-shot pulse(), decaying
  let pulseMix = 0;    // eases toward 1 while pulsing, 0 when not
  let pulseClock = 0;  // seconds of pulsing, for the swell's phase
  let surge = 0;       // what the frame uses: both combined

  function setRibbons(p: Program, box: [number, number], center: [number, number], targetWidth: number) {
    const u = p.uniforms;
    // Full-bleed swings taller with the box, so the band reaches ~70% of its
    // height. Filling, ribbons are scaled up by their frames: thin them back.
    const k = config.placement === 'full' ? Math.max(1, box[1] * 1.6) : 1;
    const fill = config.placement === 'fill';
    const opts = {
      ...config,
      amplitude: config.amplitude * k * (fill ? 1.2 : 1) * (1 + 0.3 * surge),
      spread: config.spread * k,
      thickness: config.thickness * (fill ? 0.65 : 1),
    };
    const r = ribbonParams(opts, time);
    gl.uniform2f(u.uBox, box[0], box[1]);
    gl.uniform1f(u.uPixel, box[0] / targetWidth);
    gl.uniform2f(u.uCenter, center[0], center[1]);
    gl.uniform1i(u.uCount, r.count);
    gl.uniform4fv(u['uWave[0]'], r.wave);
    gl.uniform4fv(u['uShape[0]'], r.shape);
    gl.uniform4fv(u['uFrame[0]'], ribbonFrames(opts, box, time));
    gl.uniform3fv(u['uColor[0]'], ribbonColors(PALETTES[config.palette] ?? PALETTES.flare));
    gl.uniform1f(u.uFringe, FRINGE_BASE * (FRINGE[config.palette] ?? 1));
    gl.uniform1f(u.uWhite, WHITE);
    gl.uniform4fv(u['uExtra[0]'], r.extra);
    gl.uniform1f(u.uDepth, config.depth);
    gl.uniform1f(u.uSheen, config.sheen * (SHEEN[config.palette] ?? 1));
    gl.uniform1f(u.uGlints, config.glints);
  }

  function blurPass(src: FBO, dst: FBO, taps: ReturnType<typeof gaussianTaps>, dx: number, dy: number) {
    const u = blur.uniforms;
    gl.uniform1i(u.uSrc, src.attach(0));
    gl.uniform2f(u.uStep, dx * src.texelSizeX, dy * src.texelSizeY);
    gl.uniform1i(u.uTaps, taps.count);
    gl.uniform1fv(u['uOffset[0]'], taps.offsets);
    gl.uniform1fv(u['uWeight[0]'], taps.weights);
    blit(dst);
  }

  function frame(dt: number, rawDtMs: number) {
    if (reducedMotion.matches || !config.motion) {
      // One static frame: redraw only when the config or size changes.
      reveal = 1;
      const key = JSON.stringify(config);
      if (key === drawn) return;
      drawn = key;
    } else {
      drawn = '';
      energy *= Math.exp((-PULSE_DECAY * rawDtMs) / 1000);
      pulseMix += ((config.pulsing ? 1 : 0) - pulseMix) * Math.min(1, rawDtMs / 500);
      if (pulseMix > 0.001) pulseClock += rawDtMs / 1000;
      else pulseClock = 0;
      // A smooth swell from rest, peaking once per period.
      const swell = (0.5 - 0.5 * Math.cos((pulseClock / PULSE_PERIOD) * Math.PI * 2)) ** 2;
      surge = energy + pulseMix * (0.25 + 0.6 * swell);
      time += dt * config.speed * (1 + 2 * surge);
      reveal = Math.min(1, reveal + rawDtMs / REVEAL_MS);
      if (config.grainPeriod > 0) grainClock += rawDtMs / 1000 / config.grainPeriod;
    }

    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    // Half the distance between the pinch points, in CSS px. Centered, the
    // band takes most of a landscape box but stays clear of the edges;
    // full-bleed, the ends sit just past the sides. Filling, a unit is half
    // the width and each ribbon sizes its own frame.
    const half = config.placement === 'full' ? w * 0.56 : config.placement === 'fill' ? w * 0.5 : Math.min(w * 0.4, h * 0.85);
    const box: [number, number] = [w / half, h / half];
    const center: [number, number] = [0.5, 0.5];

    seed.use();
    setRibbons(seed, box, center, halo!.width);
    blit(halo);
    blur.use();
    blurPass(halo!, haloScratch!, haloTaps, 1, 0);
    blurPass(haloScratch!, halo!, haloTaps, 0, 1);
    // Eighth resolution: one bilinear tap averages 2×2 halo texels.
    blurPass(halo!, wide!, copyTaps, 0, 0);
    for (let k = 0; k < 2; k++) {
      blurPass(wide!, wideScratch!, wideTaps, 1, 0);
      blurPass(wideScratch!, wide!, wideTaps, 0, 1);
    }

    composite.use();
    setRibbons(composite, box, center, canvas.width);
    const u = composite.uniforms;
    gl.uniform1i(u.uHalo, halo!.attach(0));
    gl.uniform1i(u.uWide, wide!.attach(1));
    gl.uniform1f(u.uGlow, config.glow * (1 + 0.8 * surge));
    // Breathes: a slow swell in brightness on top of the ribbons' own.
    const breath = 0.92 + 0.08 * Math.sin(time * 0.55 + 0.6);
    gl.uniform1f(u.uBright, config.intensity * breath * (1 + 0.5 * surge) * (1 - Math.pow(1 - reveal, 3)));
    setGrain(gl, u, config.grain, grainClock);
    blit(null);
  }

  // Draw black right away so the canvas never flashes before the first frame.
  frame(0, 0);
  stage.start(frame);

  return {
    config,
    pulse(strength = 1) {
      // Reduced motion keeps its one still frame.
      if (!reducedMotion.matches && config.motion) energy = Math.min(energy + strength, 2);
    },
    destroy() {
      stage.destroy(); // losing the context frees every GL object with it
    },
  };
}
