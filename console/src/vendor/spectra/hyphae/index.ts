// Vendored from wevial/spectra@4d4c754 backgrounds/hyphae/src/index.ts (MIT, see ../LICENSE), given a motion option and a held regrowth that keeps its last frame.
// Hyphae background — the mycelium network without the cursor. A glowing
// fungal web on near-black grows toward invisible nutrient patches that
// drift, swell and fade on their own: veins reach and thicken where there's
// food, fuse into loops, then thin and retreat as it runs out, while spores
// keep landing and sprouting. Ignores the pointer.

import { Program, VERTEX, makeBlit, createFBO, createDoubleFBO } from '../core/gl';
import { createStage } from '../core/stage';
import { setGrain } from '../core/glsl';
import { random } from '../core/random';
import type { FBO, DoubleFBO } from '../core/gl';
import { AGENT_STEP, DEPOSIT_VERT, DIFFUSE } from '../mycelium/shaders';
import { AGENT_INIT, FOOD, SPORE, DEPOSIT_FRAG, SEED, BLUR, COMPOSITE } from './shaders';
import { PALETTES, patches, seedClusters, gaussianTaps, hexToLinear } from './params';
import type { PaletteName } from './params';

export { PALETTES } from './params';
export type { PaletteName } from './params';

export const DEFAULTS = {
  palette: 'spectral' as PaletteName,
  /** Nutrient patches, 1–8. More food, a busier web. */
  food: 5,
  /** Seconds for one patch to swell, fade and move on. */
  cycle: 70,
  /** How hard the network reaches for food. */
  appetite: 0.6,
  /** Scent laid away from food, relative to on it, 0..1; lower starves bare veins faster. */
  barren: 0.3,
  /** Share of agents that land somewhere new as spores, per second. */
  spores: 0.004,
  /** Agents per grid cell. Changing it re-seeds the colony. */
  density: 0.5,
  /** Grid cell, CSS px. Bigger cells grow coarser veins. Grows on very large screens to stay fast. */
  cell: 1,
  /** Sensor spread, radians. */
  sensorAngle: 0.4,
  /** How far ahead agents sense, cells. */
  sensorDist: 14,
  /** Turn per step, radians. */
  turn: 0.785,
  /** Random heading jitter per step, radians. */
  wander: 0.15,
  /** Growth speed: simulation steps per second, in 60ths. */
  speed: 1,
  deposit: 0.1,
  /** Scent kept per step, 0..1. */
  decay: 0.9,
  /** Scent-to-brightness. */
  gain: 1.1,
  /** Bloom strength. */
  glow: 1,
  /** Film grain. */
  grain: 0.02,
  /** Seconds for the grain to morph into a new pattern; 0 freezes it. */
  grainPeriod: 1.5,
  /** Picks where food appears. */
  seed: 0,
  /** false grows the colony unseen and holds one still frame, as reduced motion does. */
  motion: true,
};

export type HyphaeConfig = typeof DEFAULTS;

export interface HyphaeHandle {
  /** Live config: mutate fields and the next frame picks them up. */
  config: HyphaeConfig;
  /** Clear the plate and bloom a fresh colony. */
  reset(): void;
  destroy(): void;
}

/** Upper bound on grid cells (and so agents), for very large screens. */
const MAX_CELLS = 1.1e6;
/** Food is smooth: an eighth of CSS resolution is plenty. */
const FOOD_DIV = 8;
/** Steps a reduced-motion colony grows, unseen, before its one still frame (~25s of growth). */
const WARM_STEPS = 1500;
/** Of which this many per frame. */
const WARM_RATE = 50;
const REVEAL_MS = 1500;
/** Seconds of spores to batch into one pass. */
const SPORE_EVERY = 0.25;
const HALO_SIGMA = 2;
/** How far `prism` splits red from blue, field cells either way. */
const PRISM_SPLIT = 2.4;
const WIDE_SIGMA = 4;

/**
 * Mount the effect into `container` (which should be positioned and sized).
 * Without WebGL2 + float targets the container gets the `hyphae-fallback`
 * class instead.
 */
export interface HyphaeOptions extends Partial<HyphaeConfig> {
  /**
   * Steps to grow the colony unseen before it settles into real time,
   * WARM_RATE a frame, then appear at once without the fade: a host that
   * keeps the effect hidden that long can open on a grown network.
   */
  warm?: number;
  /**
   * Draw over the page instead of the palette's ground: unlit areas are see-through and
   * the light tints whatever is behind the canvas. Fixed at mount. Default false.
   */
  transparent?: boolean;
}

export function mount(container: HTMLElement, { warm: ahead = 0, transparent = false, ...opts }: HyphaeOptions = {}): HyphaeHandle {
  const config: HyphaeConfig = { ...DEFAULTS, ...opts };

  let onResize = () => {};
  const stage = createStage(container, { fallback: 'hyphae-fallback', floatTargets: true, held: () => !config.motion, transparent, onResize: () => onResize() });
  if (!stage) {
    return { config, reset() {}, destroy() {} };
  }
  const { gl, canvas, reducedMotion } = stage;
  const blit = makeBlit(gl);
  const init = new Program(gl, VERTEX, AGENT_INIT);
  const step = new Program(gl, VERTEX, AGENT_STEP);
  const spore = new Program(gl, VERTEX, SPORE);
  const deposit = new Program(gl, DEPOSIT_VERT, DEPOSIT_FRAG);
  const diffuse = new Program(gl, VERTEX, DIFFUSE);
  const feed = new Program(gl, VERTEX, FOOD);
  const seedProg = new Program(gl, VERTEX, SEED);
  const blur = new Program(gl, VERTEX, BLUR);
  const composite = new Program(gl, VERTEX, COMPOSITE);
  const haloTaps = gaussianTaps(HALO_SIGMA);
  const wideTaps = gaussianTaps(WIDE_SIGMA);
  const copyTaps = { count: 1, offsets: new Float32Array(8), weights: new Float32Array([1, 0, 0, 0, 0, 0, 0, 0]) };

  const points = gl.createVertexArray();
  const indexBuf = gl.createBuffer();

  let field: DoubleFBO | null = null;
  let agents: DoubleFBO | null = null;
  let food: FBO | null = null;
  let halo: FBO | null = null, haloScratch: FBO | null = null;
  let wide: FBO | null = null, wideScratch: FBO | null = null;
  let side = 0;
  let cell = 0;
  let density = 0;
  let sized = '';

  // Everything but the agents is sized from CSS px, so it survives the
  // stage's adaptive resolution changes untouched.
  function makeTargets() {
    const cw = canvas.clientWidth || 1, ch = canvas.clientHeight || 1;
    cell = config.cell;
    const size = Math.max(cell, Math.sqrt(cw * ch / MAX_CELLS));
    const fw = Math.max(1, Math.round(cw / size)), fh = Math.max(1, Math.round(ch / size));
    const key = `${fw}x${fh}`;
    if (key === sized) return false;
    sized = key;
    field?.dispose();
    field = createDoubleFBO(gl, fw, fh, gl.R16F, gl.RED, gl.HALF_FLOAT, gl.LINEAR);
    // The plate wraps (agents do too), so the blur and sensors must as well.
    for (const fbo of [field.read, field.write]) {
      fbo.attach(0);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT);
    }
    for (const f of [food, halo, haloScratch, wide, wideScratch]) f?.dispose();
    const target = (div: number) => createFBO(gl,
      Math.max(1, Math.round(cw / div)), Math.max(1, Math.round(ch / div)),
      gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
    food = target(FOOD_DIV);
    halo = target(4);
    haloScratch = target(4);
    wide = target(8);
    wideScratch = target(8);
    return true;
  }

  let time = 0;
  let ticks = 0; // steps run, for fresh randomness each step even while time stands still
  let carry = 0;
  let warm = 0;
  let reveal = 0;
  let grainClock = 0;
  let sporeDue = 0; // seconds of spores owed
  let drawn = '';
  let shown = false;

  function reset() {
    sized = '';
    makeTargets();
    density = config.density;
    const f = field!;
    side = Math.max(16, Math.ceil(Math.sqrt(f.width * f.height * density)));
    agents?.dispose();
    agents = createDoubleFBO(gl, side, side, gl.RGBA32F, gl.RGBA, gl.FLOAT, gl.NEAREST);
    const clusters = seedClusters(random, 4);
    init.use();
    gl.uniform1f(init.uniforms.uAspect, f.width / f.height);
    gl.uniform1f(init.uniforms.uSeed, random() * 1000);
    gl.uniform1i(init.uniforms.uClusters, clusters.count);
    gl.uniform4fv(init.uniforms['uCluster[0]'], clusters.data);
    blit(agents.read);

    gl.bindVertexArray(points);
    gl.bindBuffer(gl.ARRAY_BUFFER, indexBuf);
    gl.bufferData(gl.ARRAY_BUFFER, Float32Array.from({ length: side * side }, (_, i) => i), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 1, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    warm = WARM_STEPS;
    drawn = '';
  }

  // Agents live in uv space, so they survive a resize; only the scent restarts.
  onResize = () => {
    if (field && makeTargets()) warm = WARM_STEPS;
    drawn = '';
    shown = false;
  };

  function blurPass(src: FBO, dst: FBO, taps: ReturnType<typeof gaussianTaps>, dx: number, dy: number) {
    const u = blur.uniforms;
    gl.uniform1i(u.uSrc, src.attach(0));
    gl.uniform2f(u.uStep, dx * src.texelSizeX, dy * src.texelSizeY);
    gl.uniform1i(u.uTaps, taps.count);
    gl.uniform1fv(u['uOffset[0]'], taps.offsets);
    gl.uniform1fv(u['uWeight[0]'], taps.weights);
    blit(dst);
  }

  function simulate(steps: number) {
    const f = field!;
    const a = agents!;
    const aspect = f.width / f.height;

    const p = patches(time, config.food, config.seed, config.cycle);
    feed.use();
    gl.uniform1f(feed.uniforms.uAspect, aspect);
    gl.uniform1i(feed.uniforms.uCount, p.count);
    gl.uniform4fv(feed.uniforms['uPatch[0]'], p.data);
    gl.uniform1f(feed.uniforms.uTime, time);
    blit(food);

    for (let i = 0; i < steps; i++) {
      step.use();
      let u = step.uniforms;
      gl.uniform1i(u.uAgents, a.read.attach(0));
      gl.uniform1i(u.uField, f.read.attach(1));
      gl.uniform1i(u.uTrail, food!.attach(2));
      gl.uniform2f(u.uCells, f.width, f.height);
      gl.uniform1f(u.uSensorAngle, config.sensorAngle);
      gl.uniform1f(u.uSensorDist, config.sensorDist);
      gl.uniform1f(u.uTurn, config.turn);
      gl.uniform1f(u.uSpeed, 1);
      gl.uniform1f(u.uAttract, config.appetite);
      gl.uniform1f(u.uWander, config.wander);
      gl.uniform1f(u.uSeed, (++ticks * 13.17) % 1000);
      blit(a.write);
      a.swap();

      deposit.use();
      u = deposit.uniforms;
      gl.uniform1i(u.uAgents, a.read.attach(0));
      gl.uniform1i(u.uSide, side);
      gl.uniform1i(u.uFood, food!.attach(2));
      gl.uniform2f(u.uCells, f.width, f.height);
      gl.uniform1f(u.uDeposit, config.deposit);
      gl.uniform1f(u.uBarren, config.barren);
      gl.bindFramebuffer(gl.FRAMEBUFFER, f.read.fbo);
      gl.viewport(0, 0, f.width, f.height);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);
      gl.bindVertexArray(points);
      gl.drawArrays(gl.POINTS, 0, side * side);
      gl.disable(gl.BLEND);

      diffuse.use();
      u = diffuse.uniforms;
      gl.uniform1i(u.uField, f.read.attach(1));
      gl.uniform2f(u.uTexel, 1 / f.width, 1 / f.height);
      gl.uniform1f(u.uDecay, config.decay);
      blit(f.write);
      f.swap();
    }
  }

  function setShade(p: Program) {
    const u = p.uniforms;
    const colors = (PALETTES[config.palette] ?? PALETTES.foxfire).map(hexToLinear);
    gl.uniform1i(u.uField, field!.read.attach(1));
    gl.uniform1i(u.uFood, food!.attach(2));
    gl.uniform1f(u.uGain, config.gain);
    gl.uniform1f(u.uAspect, field!.width / field!.height);
    gl.uniform1f(u.uTime, time);
    gl.uniform1f(u.uSpectral, config.palette === 'spectral' ? 1 : 0);
    gl.uniform3fv(u.uHaze, colors[1]);
    gl.uniform3fv(u.uHypha, colors[2]);
    gl.uniform3fv(u.uTip, colors[3]);
    gl.uniform2f(u.uCell, 1 / field!.width, 1 / field!.height);
    return colors;
  }

  function frame(dt: number, rawDtMs: number) {
    if (config.density !== density) reset();
    else if (config.cell !== cell && makeTargets()) warm = WARM_STEPS;

    let steps: number;
    let elapsed: number; // seconds of colony life this frame
    const held = reducedMotion.matches || !config.motion;
    if (held) {
      // Fast-forward the colony unseen, food and spores included, then hold one still frame.
      steps = Math.min(warm, WARM_RATE);
      elapsed = steps / 60;
      warm -= steps;
      reveal = warm ? 0 : 1;
      const key = JSON.stringify(config);
      if (!steps && key === drawn) return;
      drawn = warm ? '' : key;
    } else if (ahead > 0) {
      warm = 0;
      steps = Math.min(ahead, WARM_RATE);
      elapsed = steps / 60 / Math.max(config.speed, 0.01);
      ahead -= steps;
      reveal = ahead ? 0 : 1;
    } else {
      warm = 0;
      elapsed = dt;
      carry += config.speed * dt * 60;
      steps = Math.min(Math.floor(carry), 6);
      carry -= steps;
      reveal = Math.min(1, reveal + rawDtMs / REVEAL_MS);
      if (config.grainPeriod > 0) grainClock += rawDtMs / 1000 / config.grainPeriod;
    }
    time += elapsed;
    sporeDue += elapsed;
    if (sporeDue >= SPORE_EVERY) {
      // Spores: a few agents relocate at random. Without them (and moving
      // food) the network would shrink to a bare tree between the patches.
      // Batched, since each pass rewrites every agent to move a handful.
      spore.use();
      gl.uniform1i(spore.uniforms.uAgents, agents!.read.attach(0));
      gl.uniform1f(spore.uniforms.uChance, config.spores * sporeDue);
      sporeDue = 0;
      gl.uniform1f(spore.uniforms.uSeed, (ticks * 7.31 + 0.5) % 1000);
      blit(agents!.write);
      agents!.swap();
    }
    simulate(steps);
    // A held colony regrowing unseen leaves its last frame on the canvas rather than a blank ground.
    if (held && warm && shown) return;

    seedProg.use();
    setShade(seedProg);
    blit(halo);
    blur.use();
    blurPass(halo!, haloScratch!, haloTaps, 1, 0);
    blurPass(haloScratch!, halo!, haloTaps, 0, 1);
    blurPass(halo!, wide!, copyTaps, 0, 0);
    for (let k = 0; k < 2; k++) {
      blurPass(wide!, wideScratch!, wideTaps, 1, 0);
      blurPass(wideScratch!, wide!, wideTaps, 0, 1);
    }

    composite.use();
    const colors = setShade(composite);
    const u = composite.uniforms;
    gl.uniform1i(u.uHalo, halo!.attach(3));
    gl.uniform1i(u.uWide, wide!.attach(4));
    gl.uniform1f(u.uGlow, config.glow);
    gl.uniform1f(u.uBright, 1 - Math.pow(1 - reveal, 3));
    gl.uniform3fv(u.uGround, transparent ? [0, 0, 0] : colors[0]); // transparent: the page is the ground
    // Prism: the split turns slowly, so strands at every angle get their turn to fan out.
    const ang = 0.7 + time * 0.012;
    gl.uniform1f(u.uPrism, config.palette === 'prism' ? 1 : 0);
    gl.uniform2f(u.uSplit, Math.cos(ang) * PRISM_SPLIT / field!.width, Math.sin(ang) * PRISM_SPLIT / field!.height);
    setGrain(gl, u, config.grain, grainClock);
    blit(null);
    shown = reveal > 0;
  }

  reset();
  // Draw the ground right away so the canvas never flashes before the first frame.
  frame(0, 0);
  stage.start(frame);

  return {
    config,
    reset,
    destroy() {
      stage.destroy(); // losing the context frees every GL object with it
    },
  };
}
