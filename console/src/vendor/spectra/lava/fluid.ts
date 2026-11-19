// Vendored from wevial/spectra@4d4c754 backgrounds/lava/src/fluid.ts (MIT, see ../LICENSE).
// The lamp's physics: a two-fluid simulation on the GPU (see shaders.ts for
// each pass). Wax and liquid share one velocity field; the wax is a
// fraction φ that the flow carries, kept crisp by a conservative sharpening
// step. Warm wax is buoyant and cold wax sinks, surface tension rounds wax
// into blobs and pinches thin necks into drops, and heat comes from a few
// hot spots wandering along the bulb. Pools, plumes, necks, splats against
// the top, drips and merges all come out of that; none of it is scripted.

import { Program, VERTEX, createFBO, createDoubleFBO, makeBlit } from '../core/gl';
import type { FBO, DoubleFBO, Blit } from '../core/gl';
import {
  INIT, ADVECT, VISCOSITY, FORCES, HEAT, SHARPEN, MASS, DIVERGENCE, PRESSURE, SUBTRACT, BLUR, RENDER,
  MAX_SPOTS, MAX_SEEDS,
} from './shaders';

/** Physics constants, in cells (the box is ROWS cells tall) and seconds. Exported for tuning. */
export const P = {
  ROWS: 160,
  GRAVITY: 80,       // cells/s² per unit temperature off neutral
  TENSION: 700,      // surface tension: strong, so wax rises in thick ribbons and round blobs
  VISC_WAX: 0.45,    // per-pass velocity smoothing inside wax: thick, so blobs hold together
  VISC_PASSES: 1,    // smoothing passes per step; more makes the wax resist stretching
  VISC_LIQUID: 0.03,
  DAMP: 1.5,         // 1/s
  FLOOR_HOLD: 6,     // cells above the floor where cool wax rests on the coil
  LIQUID_LIFT: 40,   // cells/s²: liquid's lift relative to wax near the bulb, applied as wax settling
  RELEASE: 0.6,      // temperature at which wax resting on the coil lets go
  CONDUCT: 0.01,     // per-step smoothing of temperature through the wax
  HEAT: 10,          // 1/s at a hot spot
  ZONE: 0.06,        // height the bulb warms
  SPOT_W: 0.09,      // hot spot half-width, fraction of the width
  BASE_HEAT: 0.4,    // the bulb's warmth between the spots, relative to a spot
  COOL_WAX: 0.006,   // 1/s through a blob's bulk
  COOL_SURFACE: 0.06, // 1/s more where wax meets the liquid
  AMBIENT: 0.2,      // liquid temperature at the floor (0 at the top; wax is neutral at 0.5)
  SHARP_EPS: 0.6,    // edge width, cells
  SHARP_STEP: 0.08,  // sharpening per step: gentle, so surface tension keeps blobs round
  SHARP_PRESS: 1,    // compression share of the sharpening
  HOLD: 0.4,         // how hard edges grow or shrink to hold the wax's total, per 1/60s
  PRESSURE_ITERS: 30,
  MAX_DT: 1 / 60,    // longer steps shatter thin wax into spray
};

export interface Spot { x: number; phase: number; speed: number; strength: number }

function rng(seed: number) {
  let s = (Math.imul(seed + 1, 2654435761) ^ 0x9e3779b9) >>> 0 || 1;
  return () => {
    s ^= s << 13; s >>>= 0;
    s ^= s >>> 17;
    s ^= s << 5; s >>>= 0;
    return s / 4294967296;
  };
}

/** Where the hot spots sit at time t: each wanders its own lane, overlapping its neighbors'. */
export function placeSpots(spots: Spot[], t: number) {
  const n = spots.length;
  for (let k = 0; k < n; k++) {
    const s = spots[k];
    const lane = (k + 0.5) / n;
    s.x = Math.min(0.95, Math.max(0.05,
      lane + (Math.sin(t * s.speed + s.phase) * 0.7 + Math.sin(t * s.speed * 2.3 + s.phase * 3) * 0.3) * 0.8 / n));
    s.strength = 0.55 + 0.45 * Math.sin(t * 0.13 + s.phase * 2);
  }
}

export interface Measure {
  /** Wax in the lamp, as a fraction of the box. */
  wax: number;
  /** Fraction of the wax in the bottom fifth, middle and top fifth. */
  bottom: number; middle: number; top: number;
  /** Mean temperature of the wax, of the wax on the floor (bottom twentieth), and the hottest wax. */
  temp: number; floorTemp: number; hottest: number;
  /** Fastest speed in the lamp, box heights per second. */
  maxSpeed: number;
  /** Ten bands from the floor up: the share of the wax in each, and its mean temperature. */
  bands: { wax: number; temp: number }[];
  /**
   * How thick the wax is away from the pool and the top: the widths of the
   * horizontal runs of wax between a quarter and 85% of the height, in box
   * heights (median, and the thinnest quarter's). 0 when there is none.
   */
  ribbon: { median: number; thin: number };
}

export class LavaFluid {
  readonly gl: WebGL2RenderingContext;
  time = 0;
  aspect = 1;
  spots: Spot[] = [];
  private blit: Blit;
  private pr: Record<string, Program>;
  private velocity!: DoubleFBO;
  private state!: DoubleFBO;
  private pressure!: DoubleFBO;
  private divergence!: FBO;
  private flux!: FBO;
  private mass!: FBO;
  private massLod = 0;
  private target = 0.1;
  private body!: DoubleFBO;
  private bodyDirty = true;
  private pairs: { a: FBO; b: FBO; target: FBO }[] = [];
  private spotData = new Float32Array(MAX_SPOTS * 2);
  private cols = 0;

  constructor(gl: WebGL2RenderingContext) {
    this.gl = gl;
    this.blit = makeBlit(gl);
    this.pr = {
      init: new Program(gl, VERTEX, INIT),
      advect: new Program(gl, VERTEX, ADVECT),
      viscosity: new Program(gl, VERTEX, VISCOSITY),
      forces: new Program(gl, VERTEX, FORCES),
      heat: new Program(gl, VERTEX, HEAT),
      sharpen: new Program(gl, VERTEX, SHARPEN),
      mass: new Program(gl, VERTEX, MASS),
      divergence: new Program(gl, VERTEX, DIVERGENCE),
      pressure: new Program(gl, VERTEX, PRESSURE),
      subtract: new Program(gl, VERTEX, SUBTRACT),
      blur: new Program(gl, VERTEX, BLUR),
      render: new Program(gl, VERTEX, RENDER),
    };
    // Sampler units belong to the program and never change with ping-pong.
    const samplers = {
      advect: { uVelocity: 0, uState: 1 },
      viscosity: { uVelocity: 0, uState: 1 },
      forces: { uVelocity: 0, uState: 1 },
      heat: { uState: 0 }, mass: { uState: 0 },
      sharpen: { uState: 0, uFlux: 1, uMass: 2 },
      divergence: { uVelocity: 0 }, pressure: { uPressure: 0, uDivergence: 1 },
      subtract: { uPressure: 0, uVelocity: 1 }, blur: { uSource: 0 },
      render: { uState: 0, uBody: 1 },
    };
    for (const [name, units] of Object.entries(samplers)) {
      const p = this.pr[name];
      p.use();
      for (const [uniform, unit] of Object.entries(units)) gl.uniform1i(p.uniforms[uniform], unit);
    }
  }

  /** Fill a lamp `aspect` wide with `amount` (fraction of the box) of wax. */
  fill(seed: number, aspect: number, amount: number) {
    const gl = this.gl;
    const rand = rng(seed);
    this.aspect = aspect;
    this.target = amount;
    this.allocate(aspect);
    this.time = rand() * 100;

    const count = Math.min(MAX_SPOTS, Math.max(1, Math.round(aspect * 1.6)));
    this.spots = Array.from({ length: count }, () => ({ x: 0, phase: rand() * 6.283, speed: 0.04 + rand() * 0.04, strength: 1 }));
    placeSpots(this.spots, this.time);
    this.spotData.fill(0);
    this.bodyDirty = true;

    // About a third of the wax starts in blobs, warm low and cool high; the rest pools.
    let total = amount * aspect;
    const seeds: number[] = [];
    let left = total * 0.35;
    while (left > 0.003 && seeds.length < MAX_SEEDS * 4) {
      const r = Math.min(Math.sqrt(left / Math.PI), 0.04 + rand() * 0.05);
      const y = 0.3 + rand() * 0.55;
      seeds.push((0.08 + rand() * 0.84) * aspect, y, r, y < 0.55 ? 0.8 : 0.25);
      left -= Math.PI * r * r;
    }
    total -= total * 0.35 - left;

    const p = this.pr.init;
    p.use();
    gl.uniform1f(p.uniforms.uAspect, aspect);
    gl.uniform1f(p.uniforms.uPool, total / aspect);
    gl.uniform4fv(p.uniforms['uSeeds[0]'], new Float32Array(seeds.concat(Array(MAX_SEEDS * 4).fill(0)).slice(0, MAX_SEEDS * 4)));
    gl.uniform1i(p.uniforms.uSeedCount, seeds.length / 4);
    this.blit(this.state.write);
    this.state.swap();
    this.clear(this.velocity);
    this.clear(this.pressure);
  }

  private clear(target: DoubleFBO) {
    const gl = this.gl;
    for (const f of [target.read, target.write]) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, f.fbo);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
    }
  }

  private allocate(aspect: number) {
    const gl = this.gl;
    const cols = Math.max(16, Math.round(P.ROWS * aspect));
    if (cols === this.cols) return;
    for (const f of [this.velocity, this.state, this.pressure, this.body]) f?.dispose();
    this.divergence?.dispose();
    this.flux?.dispose();
    this.mass?.dispose();
    for (const pair of this.pairs) gl.deleteFramebuffer(pair.target.fbo);
    this.pairs.length = 0;
    this.cols = cols;
    const rows = P.ROWS;
    this.velocity = createDoubleFBO(gl, cols, rows, gl.RG16F, gl.RG, gl.HALF_FLOAT, gl.LINEAR);
    // Full float where it can be filtered: GPUs may round half floats toward
    // zero on write, which slowly erodes the wax at every edge.
    const full = !!gl.getExtension('OES_texture_float_linear');
    this.state = full
      ? createDoubleFBO(gl, cols, rows, gl.RGBA32F, gl.RGBA, gl.FLOAT, gl.LINEAR)
      : createDoubleFBO(gl, cols, rows, gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
    this.pressure = createDoubleFBO(gl, cols, rows, gl.R16F, gl.RED, gl.HALF_FLOAT, gl.NEAREST);
    this.divergence = createFBO(gl, cols, rows, gl.R16F, gl.RED, gl.HALF_FLOAT, gl.NEAREST);
    this.flux = createFBO(gl, cols, rows, gl.RG16F, gl.RG, gl.HALF_FLOAT, gl.NEAREST);
    this.body = createDoubleFBO(gl, cols, rows, gl.R16F, gl.RED, gl.HALF_FLOAT, gl.LINEAR);
    // φ resampled to a power-of-two texture, whose mipmaps average it exactly, down to one texel.
    this.mass = createFBO(gl, 256, 128, gl.R16F, gl.RED, gl.HALF_FLOAT, gl.NEAREST);
    gl.bindTexture(gl.TEXTURE_2D, this.mass.texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST_MIPMAP_NEAREST);
    this.massLod = 8;
    for (const p of Object.values(this.pr)) {
      p.use();
      if (p.uniforms.uTexel) gl.uniform2f(p.uniforms.uTexel, 1 / cols, 1 / rows);
    }
    const p = this.pr.render;
    p.use();
    gl.uniform2f(p.uniforms.uSimSize, cols, rows);
  }

  private blitPair(a: FBO, b: FBO) {
    // Keep attachment pairs intact across ping-pong; reattaching every step
    // would make the driver revalidate them. Only the framebuffer is owned here.
    for (const pair of this.pairs) {
      if (pair.a === a && pair.b === b) { this.blit(pair.target); return; }
    }
    const gl = this.gl;
    const target = { ...a, fbo: gl.createFramebuffer()! };
    gl.bindFramebuffer(gl.FRAMEBUFFER, target.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, a.texture, 0);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT1, gl.TEXTURE_2D, b.texture, 0);
    gl.drawBuffers([gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1]);
    this.pairs.push({ a, b, target });
    this.blit(target);
  }

  /** The grid only changes with the box's shape; a new shape refills the lamp. */
  needsFill(aspect: number) {
    return Math.max(16, Math.round(P.ROWS * aspect)) !== this.cols;
  }

  /** Advance by dt seconds, in steps no longer than MAX_DT. `heat` scales the bulb. */
  advance(dt: number, heat: number) {
    for (let left = dt; left > 1e-6; left -= P.MAX_DT) this.step(Math.min(left, P.MAX_DT), heat);
  }

  private step(dt: number, heat: number) {
    const { gl, pr, velocity, state, pressure, blit } = this;
    this.time += dt;
    placeSpots(this.spots, this.time);
    gl.disable(gl.BLEND);

    let p = pr.advect;
    p.use();
    gl.uniform1f(p.uniforms.uDt, dt);
    velocity.read.attach(0);
    state.read.attach(1);
    this.blitPair(state.write, velocity.write);
    state.swap();
    velocity.swap();

    p = pr.viscosity;
    p.use();
    state.read.attach(1);
    gl.uniform1f(p.uniforms.uViscWax, P.VISC_WAX);
    gl.uniform1f(p.uniforms.uViscLiquid, P.VISC_LIQUID);
    for (let i = 0; i < P.VISC_PASSES; i++) {
      velocity.read.attach(0);
      blit(velocity.write);
      velocity.swap();
    }

    p = pr.forces;
    p.use();
    gl.uniform1f(p.uniforms.uDt, dt);
    velocity.read.attach(0);
    gl.uniform1f(p.uniforms.uGravity, P.GRAVITY);
    gl.uniform1f(p.uniforms.uTension, P.TENSION);
    gl.uniform1f(p.uniforms.uDamp, P.DAMP);
    gl.uniform1f(p.uniforms.uFloorHold, P.FLOOR_HOLD);
    gl.uniform1f(p.uniforms.uLiquidLift, P.LIQUID_LIFT);
    gl.uniform1f(p.uniforms.uRelease, P.RELEASE);
    blit(velocity.write);
    velocity.swap();

    p = pr.heat;
    p.use();
    gl.uniform1f(p.uniforms.uDt, dt);
    state.read.attach(0);
    gl.uniform1f(p.uniforms.uConduct, P.CONDUCT);
    gl.uniform1f(p.uniforms.uHeat, P.HEAT * heat);
    gl.uniform1f(p.uniforms.uZone, P.ZONE);
    for (let i = 0; i < this.spots.length; i++) {
      this.spotData[i * 2] = this.spots[i].x;
      this.spotData[i * 2 + 1] = this.spots[i].strength;
    }
    gl.uniform2fv(p.uniforms['uSpots[0]'], this.spotData);
    gl.uniform1f(p.uniforms.uSpotW, P.SPOT_W);
    gl.uniform1f(p.uniforms.uBaseHeat, P.BASE_HEAT);
    gl.uniform1f(p.uniforms.uCoolWax, P.COOL_WAX);
    gl.uniform1f(p.uniforms.uCoolSurface, P.COOL_SURFACE);
    gl.uniform1f(p.uniforms.uAmbient, P.AMBIENT);
    this.blitPair(state.write, this.flux);
    state.swap();
    p = pr.mass;
    p.use();
    state.read.attach(0);
    blit(this.mass);
    this.mass.attach(2);
    gl.generateMipmap(gl.TEXTURE_2D);

    p = pr.sharpen;
    p.use();
    gl.uniform1f(p.uniforms.uMassLod, this.massLod);
    gl.uniform1f(p.uniforms.uTarget, this.target);
    gl.uniform1f(p.uniforms.uHold, P.HOLD * dt * 60);
    this.flux.attach(1);
    gl.uniform1f(p.uniforms.uEps, P.SHARP_EPS);
    gl.uniform1f(p.uniforms.uStrength, P.SHARP_STEP);
    gl.uniform1f(p.uniforms.uPress, P.SHARP_PRESS);
    blit(state.write);
    state.swap();

    p = pr.divergence;
    p.use();
    velocity.read.attach(0);
    blit(this.divergence);
    p = pr.pressure;
    p.use();
    this.divergence.attach(1);
    gl.activeTexture(gl.TEXTURE0);
    for (let i = 0; i < P.PRESSURE_ITERS; i++) {
      gl.bindTexture(gl.TEXTURE_2D, pressure.read.texture);
      // The first blit sets the VAO and viewport for the whole solve.
      if (i === 0) blit(pressure.write);
      else {
        gl.bindFramebuffer(gl.FRAMEBUFFER, pressure.write.fbo);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
      pressure.swap();
    }
    p = pr.subtract;
    p.use();
    pressure.read.attach(0);
    velocity.read.attach(1);
    blit(velocity.write);
    velocity.swap();
    this.bodyDirty = true;
  }

  /** Draw the lamp to the screen. */
  render(colors: { liquid: number[]; deep: number[]; hot: number[] }, glow: number, reveal: number, mode: 0 | 1 | 2, setGrain: (u: Record<string, WebGLUniformLocation | null>) => void) {
    const { gl, pr, body, blit } = this;
    let p: Program;
    if (this.bodyDirty) {
      const tx = 1 / this.cols, ty = 1 / P.ROWS;
      p = pr.blur;
      p.use();
      this.state.read.attach(0);
      gl.uniform2f(p.uniforms.uDir, tx, 0);
      blit(body.write);
      body.swap();
      body.read.attach(0);
      gl.uniform2f(p.uniforms.uDir, 0, ty);
      blit(body.write);
      body.swap();

      this.bodyDirty = false;
    }

    p = pr.render;
    p.use();
    this.state.read.attach(0);
    body.read.attach(1);
    gl.uniform3fv(p.uniforms.uLiquid, colors.liquid);
    gl.uniform3fv(p.uniforms.uDeep, colors.deep);
    gl.uniform3fv(p.uniforms.uHot, colors.hot);
    gl.uniform1f(p.uniforms.uGlow, glow);
    gl.uniform1f(p.uniforms.uReveal, reveal);
    gl.uniform1f(p.uniforms.uMode, mode);
    gl.uniform1f(p.uniforms.uHue, this.time * 0.05);
    setGrain(p.uniforms);
    blit(null);
  }

  /** Read the lamp back from the GPU (slow; for tuning and tests). */
  measure(): Measure {
    const { gl } = this;
    const w = this.cols, h = P.ROWS;
    const s = new Float32Array(w * h * 4), v = new Float32Array(w * h * 4);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.state.read.fbo);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.FLOAT, s);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.velocity.read.fbo);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.FLOAT, v);
    let wax = 0, bottom = 0, top = 0, temp = 0, maxSpeed = 0, floor = 0, floorTemp = 0, hottest = 0;
    const bw = new Float64Array(10), bt = new Float64Array(10);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4, phi = s[i];
      wax += phi;
      const heat = s[i + 1]; // φT
      temp += heat;
      if (phi > 0.5) hottest = Math.max(hottest, heat / phi);
      if (y < h / 20) { floor += phi; floorTemp += heat; }
      const band = Math.min(9, Math.floor((y / h) * 10));
      bw[band] += phi; bt[band] += heat;
      if (y < h / 5) bottom += phi; else if (y >= h - h / 5) top += phi;
      maxSpeed = Math.max(maxSpeed, Math.hypot(v[i], v[i + 1]));
    }
    const runs: number[] = [];
    for (let y = Math.floor(h * 0.25); y < Math.floor(h * 0.85); y++) {
      let run = 0;
      for (let x = 0; x <= w; x++) {
        if (x < w && s[(y * w + x) * 4] > 0.5) run++;
        else if (run) { runs.push(run / h); run = 0; }
      }
    }
    runs.sort((a, b) => a - b);
    const ribbon = runs.length
      ? { median: runs[runs.length >> 1], thin: runs[runs.length >> 2] }
      : { median: 0, thin: 0 };
    return { ribbon, wax: wax / (w * h), bottom: bottom / wax, middle: 1 - (bottom + top) / wax, top: top / wax, temp: temp / wax, floorTemp: floorTemp / Math.max(floor, 1e-6), hottest, maxSpeed: maxSpeed / h,
      bands: Array.from(bw, (b, k) => ({ wax: b / wax, temp: b > 0 ? bt[k] / b : 0 })) };
  }
}
