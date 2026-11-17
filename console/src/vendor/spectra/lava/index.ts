// Vendored from wevial/spectra@d34e0f8 backgrounds/lava/src/index.ts (MIT, see ../LICENSE), given a motion option.
// Lava background — a lava lamp, simulated as two fluids. The bulb warms
// the wax pooled on it; warm wax rises through the liquid in plumes that
// neck and pinch off, splats against the top, cools, drips and sinks back,
// fusing with whatever it meets. Ignores the pointer.

import { createStage } from '../core/stage';
import { setGrain } from '../core/glsl';
import { LavaFluid, type Measure } from './fluid';

/** Three colors: the liquid, the wax's deep body, and its hot, lit edge. */
export const PALETTES = {
  magma: ['#0d0503', '#b5260e', '#ff9a3c'],
  lilac: ['#07040d', '#7a2fc0', '#f58ad8'],
  acid: ['#030a05', '#3d9a18', '#e0f052'],
  ocean: ['#020812', '#1f4fd1', '#52e3e0'],
  rose: ['#0d0408', '#c2185b', '#ffb49c'],
} satisfies Record<string, [string, string, string]>;

/**
 * Liquid for the `rainbow` palette, where wax carries a hue that blends as it
 * mixes, and the `thermal` one, where wax is colored by its temperature.
 */
export const RAINBOW_LIQUID = '#08060d';

export type PaletteName = keyof typeof PALETTES | 'rainbow' | 'thermal';

export const DEFAULTS = {
  palette: 'rainbow' as PaletteName,
  /** Wax in the lamp, as a fraction of the box. Changing it refills the lamp. */
  amount: 0.15,
  /** How hard the bulb heats the wax. */
  heat: 1,
  /** Simulation speed. */
  speed: 1,
  /** Glow the bulb and the wax throw into the liquid. */
  glow: 1,
  /** Film grain; same scale as haze's. */
  grain: 0.04,
  /** Seconds for the grain to morph into a new pattern; 0 freezes it. */
  grainPeriod: 2.5,
  /** Lays out a different lamp. */
  seed: 0,
  /** false freezes the drift and grain on one frame, as reduced motion does. */
  motion: true,
};

export type LavaConfig = typeof DEFAULTS;

export interface LavaHandle {
  /** Live config: mutate fields and the next frame picks them up. */
  config: LavaConfig;
  /** Read the simulation back from the GPU: where the wax is, how warm, how fast. Slow; for tuning. */
  measure(): Measure | null;
  /**
   * Run the lamp forward by `seconds` and draw it, without waiting for
   * animation frames, so it works in a hidden tab. For tests and tuning.
   */
  advance(seconds: number): void;
  destroy(): void;
}

const REVEAL_MS = 900;
const WARMUP_S = 3;   // simulated before the first frame, so blobs open settled

function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}

/**
 * Mount the effect into `container` (which should be positioned and sized).
 * Without WebGL2 + float targets the container gets the `lava-fallback` class instead.
 */
export function mount(container: HTMLElement, opts: Partial<LavaConfig> = {}): LavaHandle {
  const config: LavaConfig = { ...DEFAULTS, ...opts };

  let drawn = '';
  const stage = createStage(container, { floatTargets: true, onResize: () => { drawn = ''; } });
  if (!stage) {
    container.classList.add('lava-fallback');
    return { config, measure: () => null, advance() {}, destroy() {} };
  }
  const { gl, canvas, reducedMotion } = stage;
  const fluid = new LavaFluid(gl);

  const aspect = () => (canvas.clientWidth || 1) / (canvas.clientHeight || 1);
  const layout = () => `${config.seed}|${config.amount}`;
  let filled = '';
  const refill = () => {
    filled = layout();
    fluid.fill(config.seed, aspect(), Math.min(Math.max(config.amount, 0.03), 0.4));
    fluid.advance(WARMUP_S, config.heat);
  };
  refill();

  let reveal = reducedMotion.matches || !config.motion ? 1 : 0;
  let grainClock = 0;

  function frame(dt: number, rawDtMs: number) {
    if (layout() !== filled || fluid.needsFill(aspect())) refill();
    if (reducedMotion.matches || !config.motion) {
      // One static frame: redraw only when the config or size changes.
      reveal = 1;
      const key = JSON.stringify(config) + aspect();
      if (key === drawn) return;
      drawn = key;
    } else {
      drawn = '';
      fluid.advance(dt * config.speed, config.heat);
      reveal = Math.min(1, reveal + rawDtMs / REVEAL_MS);
      if (config.grainPeriod > 0) grainClock += rawDtMs / 1000 / config.grainPeriod;
    }

    draw();
  }

  let palette: string;
  let colors: { liquid: number[]; deep: number[]; hot: number[] } | undefined;
  const grain = (u: Record<string, WebGLUniformLocation | null>) => setGrain(gl, u, config.grain, grainClock);

  function draw() {
    const mode = config.palette === 'rainbow' ? 1 : config.palette === 'thermal' ? 2 : 0;
    if (!colors || palette !== config.palette) {
      palette = config.palette;
      const hex = mode
        ? [RAINBOW_LIQUID, RAINBOW_LIQUID, RAINBOW_LIQUID]
        : PALETTES[config.palette as keyof typeof PALETTES] ?? PALETTES.magma;
      const [liquid, deep, hot] = hex.map(hexToRgb);
      colors = { liquid, deep, hot };
    }
    fluid.render(colors, config.glow, 1 - Math.pow(1 - reveal, 3), mode, grain);
  }

  // Draw right away so the canvas never flashes black before the first frame.
  frame(0, 0);
  stage.start(frame);

  return {
    config,
    measure: () => fluid.measure(),
    advance(seconds) {
      if (layout() !== filled || fluid.needsFill(aspect())) refill();
      fluid.advance(seconds, config.heat);
      reveal = 1;
      draw();
    },
    destroy() {
      stage.destroy(); // losing the context frees every GL object with it
    },
  };
}
