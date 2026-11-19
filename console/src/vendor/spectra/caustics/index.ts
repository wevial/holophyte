// Vendored from wevial/spectra@4d4c754 backgrounds/caustics/src/index.ts (MIT, see ../LICENSE), given a motion option.
// Caustics background — sunlight through moving water onto a pool floor.
// The surface is a sum of travelling waves; a dense mesh of it is refracted
// onto the floor once per wavelength, so light gathers into a web of
// interlocking ribbons with faint rainbow fringes where water's dispersion
// splits them.
// Ignores the pointer.

import { Program, VERTEX, makeBlit, createFBO } from '../core/gl';
import { createStage } from '../core/stage';
import { setGrain } from '../core/glsl';
import type { FBO } from '../core/gl';
import { wavelengthToRgb } from '../film/spectrum';
import { LIGHT_VS, LIGHT_FS, BLUR, DOWNSAMPLE, MOTTLE, COMPOSITE, WAVES, MAX_TAPS } from './shaders';

/**
 * Two colors: the floor in shade, and the sunlight. `rainbow` and `prism`
 * instead draw the light as colored layers on black (see LAYERS).
 */
export const PALETTES = {
  pool: ['#0a4a66', '#e8fbff'],
  lagoon: ['#0b4848', '#fff2c6'],
  shallows: ['#4a3c26', '#fff4d8'],
  abyss: ['#010516', '#62aaff'],
  noir: ['#050507', '#ffffff'],
  rainbow: ['#000000', '#ffffff'],
  prism: ['#000000', '#ffffff'],
} satisfies Record<string, [string, string]>;

export type PaletteName = keyof typeof PALETTES;

export const DEFAULTS = {
  palette: 'prism' as PaletteName,
  /** Brightness of the caustic light. */
  intensity: 0.16,
  /** Contrast of the web: higher thins the lines and darkens the cells. */
  sharpness: 1.8,
  /** Wave speed. */
  speed: 0.17,
  /** Size of the ripple cells. */
  scale: 1.4,
  /** Water depth over the floor: shallow is soft and blurry, deeper sharpens the web and folds it into crossing, doubled lines. */
  depth: 0.72,
  /** Wave steepness. */
  choppiness: 1,
  /** Rainbow fringes on the lines; 1 is real water, more exaggerates it. */
  dispersion: 6.5,
  /** Film grain. */
  grain: 0.02,
  /** Seconds for the grain to morph into a new pattern; 0 freezes it. */
  grainPeriod: 0.8,
  /** Picks the wave directions and phases. */
  seed: 0,
  /** false freezes the drift and grain on one frame, as reduced motion does. */
  motion: true,
};

export type CausticsConfig = typeof DEFAULTS;

export interface CausticsOptions extends Partial<CausticsConfig> {
  /**
   * Draw over the page instead of the pool floor: unlit areas are see-through and
   * the light tints whatever is behind the canvas. Fixed at mount. Default false.
   */
  transparent?: boolean;
}

export interface CausticsHandle {
  /** Live config: mutate fields and the next frame picks them up. */
  config: CausticsConfig;
  destroy(): void;
}

const REVEAL_MS = 900;
const TAU = Math.PI * 2;
/** Wavelengths drawn, nm: each a separate refraction of the mesh. */
const SPECTRUM = [420, 465, 510, 555, 600, 645, 690];
/** Mesh cell size in CSS px. */
const CELL_PX = 3;
/** Multisampling for the light pass: the web's lines are a few pixels wide, so their triangles need antialiasing. */
const MSAA = 4;
/**
 * Light for the palettes that draw colored layers instead of seven
 * wavelengths: each layer is the same web phase-shifted a little further, so
 * the layers run alongside each other, split apart, and add back up to white
 * where they meet. Linear RGB, balanced so each set sums to white.
 * - `rainbow`: five saturated ribbons, distinct bands of color.
 * - `prism`: a continuous spectrum, so the split is one smooth gradient.
 */
const LAYERS: Partial<Record<PaletteName, [number, number, number][]>> = {
  rainbow: balance([
    [1, 0.06, 0.1],    // red
    [1, 0.45, 0.02],   // orange
    [0.15, 1, 0.45],   // green
    [0.08, 0.35, 1],   // blue
    [0.65, 0.15, 1],   // violet
  ]),
  prism: balance(Array.from({ length: 12 }, (_, i) => wavelengthToRgb(690 - (270 * i) / 11))),
};
/**
 * Phase spread from the first layer to the last per unit of `dispersion`,
 * radians. At the default 7 the layers split at the edges of the lines and
 * merge to white at their cores.
 */
const LAYER_SPREAD = 0.086;
/**
 * The layered palettes read as glowing light on black rather than a lit
 * floor, so they add this much `sharpness` and scale `intensity`: the cores
 * burn white and the cells go dark without retuning the sliders.
 */
const LAYER_SHARPNESS = 1.4;
const LAYER_INTENSITY = 0.8;

/** Blur on the light, CSS px sigma: the sun's disk, which softens fold edges. */
const SUN_BLUR = 0.75;
/** Glow blur, sigma in quarter-resolution texels (~10 CSS px). */
const GLOW_BLUR = 2.5;
/** Wave gravity in units/s², tuned so a mid wave travels at a calm drift. */
const GRAVITY = 0.09;

function hexToLinear(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255]
    .map((c) => c < 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)) as [number, number, number];
}

function hash(n: number) {
  const s = Math.sin(n * 12.9898 + 78.233) * 43758.5453;
  return s - Math.floor(s);
}

/** Scales colors so each channel sums to 1 across them: together they make white. */
function balance(rgb: number[][]) {
  const sum = [0, 1, 2].map((c) => rgb.reduce((s, v) => s + v[c], 0));
  return rgb.map((v) => v.map((x, c) => x / sum[c]) as [number, number, number]);
}

/** Each wavelength's share of white, so flat water lights the floor white. */
function spectrumWeights() {
  return balance(SPECTRUM.map(wavelengthToRgb));
}

/** Water's index of refraction (a Cauchy fit), dispersion scaled about 589 nm. */
function ior(nm: number, dispersion: number) {
  return 1.333 + dispersion * 3059 * (1 / (nm * nm) - 1 / (589 * 589));
}

interface Wave { kx: number; ky: number; amp: number; omega: number; phase: number; spread: number }

/**
 * Travelling waves spread over every direction by the golden angle, with
 * wavelengths from 0.32 down to 0.11 units. Curvature, which is what focuses
 * light, is steepness × wavenumber, so at equal steepness the shortest waves
 * would draw everything as a two-axis grid; a narrow band instead makes a
 * lattice of equal cells. Steepness rises with wavelength (as its square
 * root) so curvature only tilts ~1.7× across the band: every wave shapes
 * cells, and they come out irregular, 3–4× apart in size. Speed follows
 * deep-water dispersion, so the web keeps re-forming instead of sliding.
 */
function makeWaves(seed: number, scale: number, choppiness: number): Wave[] {
  const out: Wave[] = [];
  for (let i = 0; i < WAVES; i++) {
    const length = 0.32 * Math.pow(0.875, i) * (0.94 + 0.12 * hash(seed * 31 + i));
    const k = TAU / (length * scale);
    const dir = i * 2.39996 + hash(seed * 17 + i + 0.5) * 0.8 + seed * 0.37;
    out.push({
      kx: Math.cos(dir) * k,
      ky: Math.sin(dir) * k,
      amp: (0.055 * Math.sqrt(length / 0.11) * choppiness) / k,
      omega: Math.sqrt(GRAVITY * k),
      phase: hash(seed * 7 + i + 0.25) * TAU,
      spread: 0.4 + 1.2 * hash(i + 11.5),
    });
  }
  return out;
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
  return { count: offsets.length, offsets: new Float32Array(offsets.concat(Array(MAX_TAPS - offsets.length).fill(0))), weights: new Float32Array(weights.concat(Array(MAX_TAPS - weights.length).fill(0))) };
}

/**
 * Mount the effect into `container` (which should be positioned and sized).
 * Without WebGL2 + float targets the container gets the `caustics-fallback`
 * class instead.
 */
export function mount(container: HTMLElement, { transparent = false, ...opts }: CausticsOptions = {}): CausticsHandle {
  const config: CausticsConfig = { ...DEFAULTS, ...opts };

  let resizeLight = () => {};
  let drawn = '';
  const stage = createStage(container, {
    fallback: 'caustics-fallback',
    floatTargets: true,
    held: () => !config.motion,
    transparent,
    onResize: () => { resizeLight(); drawn = ''; },
  });
  if (!stage) {
    return { config, destroy() {} };
  }
  const { gl, canvas, reducedMotion } = stage;
  const blit = makeBlit(gl);
  const lightProgram = new Program(gl, LIGHT_VS, LIGHT_FS);
  // Separate programs retain each blur's taps between frames.
  const sunBlur = new Program(gl, VERTEX, BLUR);
  const glowBlur = new Program(gl, VERTEX, BLUR);
  const downsample = new Program(gl, VERTEX, DOWNSAMPLE);
  const mottleProgram = new Program(gl, VERTEX, MOTTLE);
  const composite = new Program(gl, VERTEX, COMPOSITE);
  const lightUniforms = lightProgram.uniforms;
  const waveUniform = lightUniforms['uWave[0]'];
  const weights = new Float32Array(spectrumWeights().flat());
  const layerColors = new Float32Array(12 * 3);
  const etas = new Float32Array(SPECTRUM.length);

  function setBlurTaps(program: Program, taps: ReturnType<typeof gaussianTaps>) {
    program.use();
    const u = program.uniforms;
    gl.uniform1i(u.uSrc, 0);
    gl.uniform1i(u.uTaps, taps.count);
    gl.uniform1fv(u['uOffset[0]'], taps.offsets);
    gl.uniform1fv(u['uWeight[0]'], taps.weights);
  }
  setBlurTaps(glowBlur, gaussianTaps(GLOW_BLUR));
  downsample.use();
  gl.uniform1i(downsample.uniforms.uSrc, 0);
  composite.use();
  gl.uniform1i(composite.uniforms.uLight, 0);
  gl.uniform1i(composite.uniforms.uGlow, 1);
  gl.uniform1i(composite.uniforms.uMottle, 2);

  const mesh = gl.createVertexArray();
  const meshBuf = gl.createBuffer();
  const meshIdx = gl.createBuffer();
  let indexCount = 0;
  let meshCols = 0, meshRows = 0;
  // light ⇄ scratch for the sun blur; glow ⇄ glowScratch at quarter resolution.
  let light: FBO | null = null;
  let scratch: FBO | null = null;
  let glow: FBO | null = null;
  let glowScratch: FBO | null = null;
  let mottle: FBO | null = null;
  let mottleDirty = true;
  // The light is drawn multisampled, then resolved into `light` to be sampled.
  const msaaFbo = gl.createFramebuffer();
  const msaaRb = gl.createRenderbuffer();
  const supported = Array.from(gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA16F, gl.SAMPLES) as Int32Array);
  const samples = supported.find((n) => n <= MSAA) ?? 0;

  // A grid over the box plus `margin` units per side, so refraction never
  // pulls a gap in from the edge. Cells stay about CELL_PX CSS pixels.
  function buildMesh(w: number, h: number, bx: number, by: number, margin: number) {
    const cols = Math.min(720, Math.ceil((w * (1 + (2 * margin) / bx)) / CELL_PX));
    const rows = Math.min(720, Math.ceil((h * (1 + (2 * margin) / by)) / CELL_PX));
    if (cols === meshCols && rows === meshRows) return;
    meshCols = cols;
    meshRows = rows;
    const verts = new Float32Array((cols + 1) * (rows + 1) * 2);
    for (let y = 0, o = 0; y <= rows; y++) {
      for (let x = 0; x <= cols; x++) { verts[o++] = x / cols; verts[o++] = y / rows; }
    }
    const idx = new Uint32Array(cols * rows * 6);
    for (let y = 0, o = 0; y < rows; y++) {
      for (let x = 0; x < cols; x++) {
        const a = y * (cols + 1) + x, b = a + 1, c = a + cols + 1, d = c + 1;
        idx[o++] = a; idx[o++] = b; idx[o++] = c;
        idx[o++] = b; idx[o++] = d; idx[o++] = c;
      }
    }
    indexCount = idx.length;
    gl.bindVertexArray(mesh);
    gl.bindBuffer(gl.ARRAY_BUFFER, meshBuf);
    gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, meshIdx);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
    gl.bindVertexArray(null);
  }

  resizeLight = () => {
    for (const f of [light, scratch, glow, glowScratch]) f?.dispose();
    // Full drawing-buffer resolution: the lines are a few pixels wide, so a low-res light reads as pixelated.
    const w = canvas.width;
    const h = canvas.height;
    const target = (tw: number, th: number) => createFBO(gl, Math.max(1, tw), Math.max(1, th), gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
    light = target(w, h);
    scratch = target(w, h);
    glow = target(Math.round(w / 4), Math.round(h / 4));
    glowScratch = target(glow.width, glow.height);
    setBlurTaps(sunBlur, gaussianTaps(SUN_BLUR * (w / (canvas.clientWidth || w))));
    mottleDirty = true;
    if (samples > 1) {
      gl.bindRenderbuffer(gl.RENDERBUFFER, msaaRb);
      gl.renderbufferStorageMultisample(gl.RENDERBUFFER, samples, gl.RGBA16F, w, h);
      gl.bindFramebuffer(gl.FRAMEBUFFER, msaaFbo);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, msaaRb);
    }
  };
  resizeLight();

  let time = 0;
  let reveal = reducedMotion.matches || !config.motion ? 1 : 0;
  let grainClock = 0;
  let waveSeed = NaN, waveScale = NaN, waveChoppiness = NaN;
  let boxX = NaN, boxY = NaN, lastMargin = NaN, lastDepth = NaN;
  let lastWidth = 0, lastHeight = 0;
  let mottleSeed = NaN, lastDispersion = NaN;
  let lastLayers: typeof LAYERS[PaletteName];
  let waves: Wave[] = [];
  const waveData = new Float32Array(WAVES * 4);
  const wavePhases = new Float64Array(WAVES);

  // Preserve the original phase arithmetic and Float32 rounding without temporary arrays.
  function setWaves(shift: number) {
    for (let i = 0; i < WAVES; i++) waveData[i * 4 + 3] = wavePhases[i] + shift * waves[i].spread;
    gl.uniform4fv(waveUniform, waveData);
  }

  function blurPass(program: Program, src: FBO, dst: FBO, dx: number, dy: number) {
    src.attach(0);
    gl.uniform2f(program.uniforms.uStep, dx * src.texelSizeX, dy * src.texelSizeY);
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
      time += dt * config.speed;
      reveal = Math.min(1, reveal + rawDtMs / REVEAL_MS);
      if (config.grainPeriod > 0) grainClock += rawDtMs / 1000 / config.grainPeriod;
    }

    if (config.seed !== waveSeed || config.scale !== waveScale || config.choppiness !== waveChoppiness) {
      waveSeed = config.seed;
      waveScale = config.scale;
      waveChoppiness = config.choppiness;
      waves = makeWaves(config.seed, Math.max(config.scale, 0.1), config.choppiness);
      for (let i = 0; i < WAVES; i++) {
        const v = waves[i], o = i * 4;
        waveData[o] = v.kx;
        waveData[o + 1] = v.ky;
        waveData[o + 2] = v.amp;
      }
    }
    for (let i = 0; i < WAVES; i++) wavePhases[i] = waves[i].phase - waves[i].omega * time;
    const layers = LAYERS[config.palette];
    if (layers !== lastLayers) {
      lastLayers = layers;
      if (layers) for (let i = 0; i < layers.length; i++) layerColors.set(layers[i], i * 3);
    }
    if (config.dispersion !== lastDispersion) {
      lastDispersion = config.dispersion;
      for (let i = 0; i < SPECTRUM.length; i++) etas[i] = 1 / ior(SPECTRUM[i], config.dispersion);
    }

    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    // One unit: the shorter side, but at least a third of the longer, so a
    // short wide header still gets full-size ripples.
    const unit = Math.max(Math.min(w, h), Math.max(w, h) / 3);
    const bx = w / unit, by = h / unit;
    const depth = Math.max(config.depth, 0.02) * Math.max(config.scale, 0.1);
    // How far refraction can shift light sideways, with room to spare.
    const margin = Math.min(0.08 + 0.12 * depth * Math.max(config.choppiness, 0), 1);
    if (w !== lastWidth || h !== lastHeight || margin !== lastMargin) {
      buildMesh(w, h, bx, by, margin);
      lastWidth = w;
      lastHeight = h;
    }
    if (bx !== boxX || by !== boxY || margin !== lastMargin || depth !== lastDepth) {
      lightProgram.use();
      if (bx !== boxX || by !== boxY) mottleDirty = true;
      boxX = bx;
      boxY = by;
      lastMargin = margin;
      lastDepth = depth;
      gl.uniform2f(lightUniforms.uBox, bx, by);
      gl.uniform1f(lightUniforms.uMargin, margin);
      gl.uniform1f(lightUniforms.uDepth, depth);
    }
    if (config.seed !== mottleSeed) {
      mottleSeed = config.seed;
      mottleDirty = true;
    }
    if (mottleDirty) {
      // At least 256 texels per floor-space unit keeps this four-octave noise
      // smooth even at reduced DPR. Half-float filtering is WebGL2 core.
      const mw = Math.min(canvas.width, Math.max(Math.ceil(bx * 256), Math.round(canvas.width / 4)));
      const mh = Math.min(canvas.height, Math.max(Math.ceil(by * 256), Math.round(canvas.height / 4)));
      if (!mottle || mottle.width !== mw || mottle.height !== mh) {
        mottle?.dispose();
        mottle = createFBO(gl, mw, mh, gl.R16F, gl.RED, gl.HALF_FLOAT, gl.LINEAR);
      }
      mottleProgram.use();
      gl.uniform2f(mottleProgram.uniforms.uBox, bx, by);
      gl.uniform2f(mottleProgram.uniforms.uSeed, hash(config.seed + 4) * 97, hash(config.seed + 5) * 97);
      blit(mottle);
      mottle.attach(2);
      mottleDirty = false;
    }

    // Light: one refraction of the mesh per wavelength, summed.
    gl.bindFramebuffer(gl.FRAMEBUFFER, samples > 1 ? msaaFbo : light!.fbo);
    gl.viewport(0, 0, light!.width, light!.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    lightProgram.use();
    gl.bindVertexArray(mesh);
    if (layers) {
      gl.uniform1f(lightUniforms.uEta, 1 / 1.333);
      for (let i = 0; i < layers.length; i++) {
        setWaves((i / (layers.length - 1) - 0.5) * LAYER_SPREAD * config.dispersion);
        gl.uniform3fv(lightUniforms.uColor, layerColors, i * 3, 3);
        gl.drawElements(gl.TRIANGLES, indexCount, gl.UNSIGNED_INT, 0);
      }
    } else {
      setWaves(0);
      for (let i = 0; i < SPECTRUM.length; i++) {
        gl.uniform1f(lightUniforms.uEta, etas[i]);
        gl.uniform3fv(lightUniforms.uColor, weights, i * 3, 3);
        gl.drawElements(gl.TRIANGLES, indexCount, gl.UNSIGNED_INT, 0);
      }
    }
    gl.bindVertexArray(null);
    gl.disable(gl.BLEND);
    if (samples > 1) {
      gl.bindFramebuffer(gl.READ_FRAMEBUFFER, msaaFbo);
      gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, light!.fbo);
      const { width: lw, height: lh } = light!;
      gl.blitFramebuffer(0, 0, lw, lh, 0, 0, lw, lh, gl.COLOR_BUFFER_BIT, gl.NEAREST);
      gl.bindFramebuffer(gl.READ_FRAMEBUFFER, null);
    }

    // The sun's disk: blur the light, across then down.
    sunBlur.use();
    blurPass(sunBlur, light!, scratch!, 1, 0);
    blurPass(sunBlur, scratch!, light!, 0, 1);

    // The glow: a quarter-resolution copy, blurred wide enough that its texels never show.
    downsample.use();
    light!.attach(0);
    blit(glow);
    glowBlur.use();
    blurPass(glowBlur, glow!, glowScratch!, 1, 0);
    blurPass(glowBlur, glowScratch!, glow!, 0, 1);

    const colors = PALETTES[config.palette] ?? PALETTES.pool;
    composite.use();
    const u = composite.uniforms;
    light!.attach(0);
    glow!.attach(1);
    const [floor, sun] = colors.map(hexToLinear);
    gl.uniform3fv(u.uFloor, transparent ? [0, 0, 0] : floor); // transparent: the page is the floor
    gl.uniform3fv(u.uSun, sun);
    gl.uniform1f(u.uIntensity, config.intensity * (layers ? LAYER_INTENSITY : 1));
    gl.uniform1f(u.uSharpness, config.sharpness + (layers ? LAYER_SHARPNESS : 0));
    gl.uniform1f(u.uFill, layers ? 0 : 0.3);
    gl.uniform1f(u.uReveal, 1 - Math.pow(1 - reveal, 3));
    setGrain(gl, u, config.grain, grainClock);
    blit(null);
  }

  // Draw the floor right away so the canvas never flashes black before the first frame.
  frame(0, 0);
  stage.start(frame);

  return {
    config,
    destroy() {
      stage.destroy(); // losing the context frees every GL object with it
    },
  };
}
