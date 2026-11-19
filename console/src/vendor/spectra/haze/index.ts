// Vendored from wevial/spectra@75ae266 backgrounds/haze/src/index.ts (MIT, see ../LICENSE), given a motion option and a fallback when its shaders fail.
// Haze background — an ambient smoky glow, after the AgentMail console header.
// Two soft bands of smoke lean across a near-black base, bent by a slow wave
// and billowed by domain-warped noise, under live film grain, fading out
// toward the edges. Ignores the pointer.

import { Program, VERTEX, makeBlit, createFBO } from '../core/gl';
import type { FBO } from '../core/gl';
import { setGrain } from '../core/glsl';
import { createStage } from '../core/stage';
import { GLOW, COMPOSITE } from './shaders';

/**
 * Three colors: base, mid (the back band), highlight (the front band).
 * `ember` is what the AgentMail header actually displays: its #a83205 and
 * #e85d05 go through a P3 conversion that lightens and softens them.
 */
export const PALETTES: Record<string, [string, string, string]> = {
  ember: ['#120602', '#9b3b1a', '#d8662a'],
  rose: ['#12040a', '#912948', '#d65270'],
  teal: ['#020f0f', '#316d68', '#55b5a6'],
  dusk: ['#0b0612', '#6a2fb8', '#f08a3c'],
  lagoon: ['#030c12', '#1a7f8c', '#f06a9a'],
};

/** Base for the `rainbow` palette, whose band colors are a drifting Oklch hue sweep. */
export const RAINBOW_BASE = '#0a0710';

export type PaletteName = string;

export const DEFAULTS = {
  palette: 'rainbow' as PaletteName,
  /** Band opacity; past 1 the soft edges fill in. */
  intensity: 1,
  /** Drift and morph speed. */
  speed: 1,
  /** Size of the smoke bands. */
  scale: 1,
  /** Smokiness: how hard noise billows the bands. 0 is the original's clean sine bands. */
  warp: 0.35,
  /** Film grain; same scale as the `shaders` FilmGrain strength (AgentMail uses 0.1). */
  grain: 0.04,
  /** Seconds for the grain to morph into a new pattern; 0 freezes it. */
  grainPeriod: 2.5,
  /** Fade to the base over this fraction of the height, from the bottom. */
  fadeBottom: 0.8,
  /** Fade one side: negative the left, positive the right; size is the strength at that edge. */
  fadeSide: -0.6,
  /** Shifts the bands' phases and the noise; 0 is the original layout. */
  seed: 0,
  /** false freezes the drift and grain on one frame, as reduced motion does. */
  motion: true,
};

export type HazeConfig = typeof DEFAULTS;

export interface HazeHandle {
  /** Live config: mutate fields and the next frame picks them up. */
  config: HazeConfig;
  destroy(): void;
}

const REVEAL_MS = 700;
const TAU = Math.PI * 2;

function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}

function hash(n: number) {
  const s = Math.sin(n * 12.9898 + 78.233) * 43758.5453;
  return s - Math.floor(s);
}

/**
 * Mount the effect into `container` (which should be positioned and sized).
 * Without WebGL2 + float targets the container gets the `haze-fallback`
 * class instead.
 */
export function mount(container: HTMLElement, opts: Partial<HazeConfig> = {}): HazeHandle {
  const config: HazeConfig = { ...DEFAULTS, ...opts };

  let resizeGlow = () => {};
  let drawn = '';
  const stage = createStage(container, {
    floatTargets: true,
    held: () => !config.motion,
    onResize: () => { resizeGlow(); drawn = ''; },
  });
  if (!stage) {
    container.classList.add('haze-fallback');
    return { config, destroy() {} };
  }
  const { gl, canvas, reducedMotion } = stage;
  let programs: [Program, Program];
  try {
    programs = [new Program(gl, VERTEX, GLOW), new Program(gl, VERTEX, COMPOSITE)];
  } catch {
    stage.destroy();
    container.classList.add('haze-fallback');
    return { config, destroy() {} };
  }
  const [glow, composite] = programs;
  const blit = makeBlit(gl);

  // The smoke is soft, so it renders at half CSS resolution regardless of DPR.
  let light: FBO | null = null;
  resizeGlow = () => {
    light?.dispose();
    const w = Math.max(1, Math.round(canvas.clientWidth / 2));
    const h = Math.max(1, Math.round(canvas.clientHeight / 2));
    light = createFBO(gl, w, h, gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
  };
  resizeGlow();

  let time = 0;
  let reveal = reducedMotion.matches || !config.motion ? 1 : 0;
  let grainClock = 0;

  function frame(dt: number, rawDtMs: number) {
    if (reducedMotion.matches || !config.motion) {
      // One static frame: redraw only when the config or size changes.
      reveal = 1;
      const key = JSON.stringify(config);
      if (key === drawn) return;
      drawn = key;
    } else {
      drawn = '';
      time += dt * config.speed;
      reveal = Math.min(1, reveal + rawDtMs / REVEAL_MS);
      if (config.grainPeriod > 0) grainClock += rawDtMs / 1000 / config.grainPeriod;
    }

    const rainbow = config.palette === 'rainbow';
    const colors = rainbow ? [RAINBOW_BASE, RAINBOW_BASE, RAINBOW_BASE] : PALETTES[config.palette] ?? PALETTES.ember;
    const [base, mid, high] = colors.map(hexToRgb);
    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    // One unit: the height, but at most a quarter of the longer side.
    const unit = Math.min(h, Math.max(w, h) / 4) * Math.max(config.scale, 0.05);
    const s = config.seed;

    glow.use();
    let u = glow.uniforms;
    gl.uniform2f(u.uBox, w / unit, h / unit);
    gl.uniform1f(u.uTime, time);
    gl.uniform1f(u.uWarp, config.warp);
    gl.uniform2f(u.uSeed, hash(s + 4) * 97, hash(s + 5) * 97);
    if (s) gl.uniform3f(u.uPhase, hash(s) * TAU, hash(s + 1) * TAU, hash(s + 2) * TAU);
    else gl.uniform3f(u.uPhase, 0, 0, 0);
    blit(light);

    composite.use();
    u = composite.uniforms;
    gl.uniform1i(u.uGlow, light!.attach(0));
    gl.uniform3fv(u.uBase, base);
    gl.uniform3fv(u.uMid, mid);
    gl.uniform3fv(u.uHigh, high);
    gl.uniform1f(u.uIntensity, config.intensity);
    gl.uniform1f(u.uReveal, 1 - Math.pow(1 - reveal, 3));
    gl.uniform1f(u.uFadeBottom, config.fadeBottom);
    gl.uniform1f(u.uFadeSide, config.fadeSide);
    setGrain(gl, u, config.grain, grainClock);
    gl.uniform1f(u.uRainbow, rainbow ? 1 : 0);
    gl.uniform1f(u.uHue, time * 0.08 + (s ? hash(s + 6) * TAU : 0));
    blit(null);
  }

  // Draw the base right away so the canvas never flashes black before the first frame.
  frame(0, 0);
  stage.start(frame);

  return {
    config,
    destroy() {
      stage.destroy(); // losing the context frees every GL object with it
    },
  };
}
