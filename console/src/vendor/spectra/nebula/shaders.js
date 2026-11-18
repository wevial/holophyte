// Vendored from wevial/spectra@d34e0f8 cursors/nebula/src/shaders.js (MIT, see ../LICENSE).
import { COLOR, GRAIN, RAINBOW } from '../core/glsl';

// All GLSL lives here as template literals so the app runs without a bundler or fetch.

export const VERTEX = /* glsl */ `#version 300 es
precision highp float;
// A real attribute at location 0 (not gl_VertexID): drawing with attrib 0
// disabled forces slow emulation on desktop-GL browsers like Firefox/macOS.
layout(location = 0) in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

// Shared value-noise + fbm, and the procedural base nebula.
// The base nebula is the "rest state" the dye field relaxes toward; its domain
// drifts slowly with time, which is what makes the whole cloud crawl.
export const NOISE_GLSL = /* glsl */ `
float hash12(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

float vnoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash12(i);
  float b = hash12(i + vec2(1.0, 0.0));
  float c = hash12(i + vec2(0.0, 1.0));
  float d = hash12(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float v = 0.0;
  float amp = 0.5;
  mat2 rot = mat2(0.8, 0.6, -0.6, 0.8);
  for (int i = 0; i < 5; i++) {
    v += amp * vnoise(p);
    p = rot * p * 2.03;
    amp *= 0.5;
  }
  return v;
}

// Ridged fbm for filamentary structure.
float ridge(vec2 p) {
  float v = 0.0;
  float amp = 0.55;
  mat2 rot = mat2(0.8, 0.6, -0.6, 0.8);
  for (int i = 0; i < 4; i++) {
    v += amp * (1.0 - abs(2.0 * vnoise(p) - 1.0));
    p = rot * p * 2.13;
    amp *= 0.5;
  }
  return v;
}

const vec3 NEB_PINK   = vec3(1.00, 0.36, 0.75);
const vec3 NEB_PURPLE = vec3(0.55, 0.34, 0.97);
const vec3 NEB_BLUE   = vec3(0.25, 0.62, 1.00);

// Returns premultiplied nebula color in rgb and dark-dust absorption in a.
// The domain-warp offsets move at different rates from the main drift, so the
// cloud's shape slowly evolves as it crawls — a drift-through, not a slide.
vec4 baseNebula(vec2 uv, float aspect, float time) {
  vec2 p = uv * vec2(aspect, 1.0);
  vec2 drift = vec2(time * 0.009, time * -0.004);

  // Large-scale cloud mass, warped by a second fbm (domain warping).
  vec2 q = p * 1.6 + drift;
  vec2 warp = vec2(fbm(q + vec2(1.7, 9.2)), fbm(q + vec2(8.3, 2.8))) - 0.5;
  float mass = fbm(q + warp * 2.2);

  // Filamentary detail carves the cloud before thresholding.
  float fil = ridge(p * 3.1 + drift * 1.4 + warp * 1.6);
  mass += (fil - 0.55) * 0.16;

  // High threshold + power: dense glowing cores, wispy edges, real dark sky
  // between clouds.
  float density = pow(smoothstep(0.48, 0.95, mass), 1.6);
  density *= 0.4 + 1.1 * fil * fil;

  // Hue field: pink cores -> purple mid -> blue fringes.
  float hue = fbm(p * 2.2 - drift * 0.6 + vec2(4.1, 7.7));
  vec3 col = mix(NEB_BLUE, NEB_PURPLE, smoothstep(0.30, 0.52, hue));
  col = mix(col, NEB_PINK, smoothstep(0.52, 0.74, hue));

  // Hot emission core where the cloud is densest.
  float core = smoothstep(0.80, 1.0, mass + (fil - 0.5) * 0.1);
  col = mix(col, vec3(1.0, 0.75, 0.92), core * 0.8);
  density += core * core * 0.8;

  // Dark dust lanes, strongest at the cloud fringes (mid-density band).
  float dustF = fbm(p * 2.8 + warp * 1.8 + vec2(9.4, 3.1));
  float fringe = smoothstep(0.35, 0.55, mass) * (1.0 - smoothstep(0.60, 0.85, mass));
  float dust = smoothstep(0.55, 0.80, dustF) * fringe;

  return vec4(col * density, dust);
}
`;

export const ADVECTION = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uVelocity;
uniform sampler2D uSource;
uniform vec2 uTexelSize;   // texel size of the velocity field
uniform float uDt;
uniform float uDissipation;
void main() {
  vec2 coord = vUv - uDt * texture(uVelocity, vUv).xy * uTexelSize;
  outColor = texture(uSource, coord) / (1.0 + uDissipation * uDt);
}
`;

// Displacement accumulation: the offset field is carried along by the fluid,
// picks up this frame's motion, and decays toward zero — so the cloud is only
// ever *bent* by the cursor and always heals back to its untouched shape.
export const OFFSET_UPDATE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uVelocity;
uniform sampler2D uOffset;
uniform vec2 uTexelSize;   // texel size of the velocity field
uniform float uDt;
uniform float uDecay;
void main() {
  vec2 v = texture(uVelocity, vUv).xy;
  vec2 back = vUv - uDt * v * uTexelSize;
  vec2 off = texture(uOffset, back).xy;
  off += v * uTexelSize * uDt;
  off *= exp(-uDecay * uDt);
  float len = length(off);
  if (len > 0.25) off *= 0.25 / len;
  outColor = vec4(off, 0.0, 1.0);
}
`;

// The cloud, recomputed every frame: base nebula sampled through the bent
// coordinates. Divergence of the displacement drives a density response —
// stretched gas thins (cleared path), compressed gas piles up and brightens.
export const CLOUD = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uOffset;
uniform vec2 uOffTexel;
uniform float uAspect;
uniform float uTime;
${NOISE_GLSL}
void main() {
  vec2 off = texture(uOffset, vUv).xy;
  float dR = texture(uOffset, vUv + vec2(uOffTexel.x, 0.0)).x;
  float dL = texture(uOffset, vUv - vec2(uOffTexel.x, 0.0)).x;
  float dT = texture(uOffset, vUv + vec2(0.0, uOffTexel.y)).y;
  float dB = texture(uOffset, vUv - vec2(0.0, uOffTexel.y)).y;
  float div = 0.5 * ((dR - dL) / uOffTexel.x + (dT - dB) / uOffTexel.y);
  float squeeze = exp(-clamp(div, -3.0, 3.0) * 0.5);
  vec4 base = baseNebula(vUv - off, uAspect, uTime);
  outColor = vec4(base.rgb * squeeze, base.a * squeeze);
}
`;

export const SPLAT = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uTarget;
uniform float uAspect;
uniform vec2 uPoint;
uniform vec2 uDir;
uniform float uRadius;
uniform float uRadial;  // 0 = directional push, 1 = radial shove away from uPoint
void main() {
  vec2 p = vUv - uPoint;
  p.x *= uAspect;
  float g = exp(-dot(p, p) / uRadius);
  vec2 radialDir = p / (length(p) + 1e-4);
  vec2 force = mix(uDir, radialDir * length(uDir), uRadial);
  outColor = vec4(texture(uTarget, vUv).xy + force * g, 0.0, 1.0);
}
`;

export const DIVERGENCE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uVelocity;
uniform vec2 uTexelSize;
void main() {
  float L = texture(uVelocity, vUv - vec2(uTexelSize.x, 0.0)).x;
  float R = texture(uVelocity, vUv + vec2(uTexelSize.x, 0.0)).x;
  float B = texture(uVelocity, vUv - vec2(0.0, uTexelSize.y)).y;
  float T = texture(uVelocity, vUv + vec2(0.0, uTexelSize.y)).y;
  vec2 C = texture(uVelocity, vUv).xy;
  // Solid-ish boundaries: reflect velocity at the edges.
  if (vUv.x - uTexelSize.x < 0.0) L = -C.x;
  if (vUv.x + uTexelSize.x > 1.0) R = -C.x;
  if (vUv.y - uTexelSize.y < 0.0) B = -C.y;
  if (vUv.y + uTexelSize.y > 1.0) T = -C.y;
  outColor = vec4(0.5 * (R - L + T - B), 0.0, 0.0, 1.0);
}
`;

export const PRESSURE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uPressure;
uniform sampler2D uDivergence;
uniform vec2 uTexelSize;
void main() {
  float L = texture(uPressure, vUv - vec2(uTexelSize.x, 0.0)).x;
  float R = texture(uPressure, vUv + vec2(uTexelSize.x, 0.0)).x;
  float B = texture(uPressure, vUv - vec2(0.0, uTexelSize.y)).x;
  float T = texture(uPressure, vUv + vec2(0.0, uTexelSize.y)).x;
  float div = texture(uDivergence, vUv).x;
  outColor = vec4((L + R + B + T - div) * 0.25, 0.0, 0.0, 1.0);
}
`;

export const GRADIENT_SUBTRACT = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uPressure;
uniform sampler2D uVelocity;
uniform vec2 uTexelSize;
void main() {
  float L = texture(uPressure, vUv - vec2(uTexelSize.x, 0.0)).x;
  float R = texture(uPressure, vUv + vec2(uTexelSize.x, 0.0)).x;
  float B = texture(uPressure, vUv - vec2(0.0, uTexelSize.y)).x;
  float T = texture(uPressure, vUv + vec2(0.0, uTexelSize.y)).x;
  vec2 vel = texture(uVelocity, vUv).xy - vec2(R - L, T - B) * 0.5;
  outColor = vec4(vel, 0.0, 1.0);
}
`;

// Final pass: deep-space background gradient, parallax star layers whose glow
// is gated by local nebula gas, then the nebula composited on top. With
// uPaint, none of the space: the gas is laid over a flat ground as opaque
// pigment instead of glowing, like paint.
export const COMPOSITE = /* glsl */ `#version 300 es
precision highp float;
#define GLOW_GAIN 1.9
#define GLOW_CURVE 2.2
#define GLOW_BURN_LO 0.7
#define GLOW_BURN_HI 1.5
#define GLOW_WHITE 0.9
#define PRISM_SPLIT 10.0
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uDye;
uniform float uTime;
uniform float uAspect;
uniform float uPaint;   // 1: paint look
uniform vec3 uGround;   // paint look's ground color
uniform vec3 uInk[4];   // paint look's inks: fringe, middle, core, hot core
uniform float uGlow;    // glow paints: 1 inks, 2 silver prism, 3 drifting rainbow
${NOISE_GLSL}
${COLOR}
${GRAIN}
${RAINBOW}

// Deep-space vertical gradient: near-black violet up top, a touch warmer low.
const vec3 SKY_TOP = vec3(0.012, 0.004, 0.055);
const vec3 SKY_MID = vec3(0.024, 0.010, 0.078);
const vec3 SKY_BOT = vec3(0.043, 0.024, 0.110);

vec3 skyColor(float y) {
  vec3 c = mix(SKY_BOT, SKY_MID, smoothstep(0.0, 0.60, y));
  return mix(c, SKY_TOP, smoothstep(0.25, 1.0, y));
}

// --- Bright stars: dots with varied size; the glow halo only shows where
// nebula gas can diffract the light (glow = local gas density). ---
vec3 brightStars(vec2 uv, float glow) {
  vec2 g = uv * 7.0;
  vec2 id = floor(g);
  vec2 f = fract(g);
  float rnd = hash12(id);
  // The grid is world-space, so a narrow portrait screen holds fewer cells —
  // raise the spawn odds as the aspect narrows to keep the count feeling even.
  float spawn = 0.12 / clamp(uAspect, 0.45, 1.0);
  if (rnd > spawn) return vec3(0.0);
  vec2 pos = vec2(hash12(id + 17.1), hash12(id + 31.7)) * 0.5 + 0.25;
  float r = length(f - pos);
  float w = mix(500.0, 2200.0, hash12(id + 23.7)); // core sharpness varies star to star
  float core = exp(-r * r * w);
  float halo = exp(-r * r * w * 0.08) * 0.30 * glow;
  vec3 tint = mix(vec3(1.0, 0.86, 0.70), vec3(0.75, 0.85, 1.0), step(0.5, hash12(id + 3.3)));
  float amp = 0.30 + 0.70 * pow(hash12(id + 8.8), 2.0); // many dim, few bright
  return (core + halo) * tint * amp;
}

// --- Stars: parallax layers of hashed grid points, steady (no twinkle) ---
float starLayer(vec2 uv, float cells, float density) {
  vec2 g = uv * cells;
  vec2 id = floor(g);
  vec2 f = fract(g);
  float rnd = hash12(id);
  if (rnd > density) return 0.0;
  vec2 pos = vec2(hash12(id + 17.1), hash12(id + 31.7)) * 0.6 + 0.2;
  float d = length(f - pos);
  float size = 0.045 + 0.10 * hash12(id + 47.3);
  float star = smoothstep(size, 0.0, d);
  float bright = 0.35 + 0.65 * hash12(id + 5.5);
  return star * star * bright;
}

// Paint: the gas's hue picks an ink, its strength is coverage. The hue is
// read back from the gas color: red rises from blue fringes (0.25) through
// purple (0.57) to pink cores (1), and the hot cores add green. Dust lanes
// read as darker streaks in the paint.
vec3 paint(vec4 dye) {
  float m = max(max(dye.r, dye.g), dye.b);
  vec3 n = dye.rgb / max(m, 1e-4);
  float toMid = smoothstep(0.25, 0.57, n.r);
  float toCore = smoothstep(0.6, 1.0, n.r);
  float hot = smoothstep(0.45, 0.75, n.g) * toCore;
  vec3 pigment = mix(mix(uInk[0], uInk[1], toMid), uInk[2], toCore);
  pigment = mix(pigment, uInk[3], hot);
  float cover = 1.0 - exp(-m * 2.6);
  vec3 col = mix(uGround, pigment, cover);
  return col * (1.0 - dye.a * 0.35);
}

// Glow: the gas emitted as light over a dark ground. Its color is the inks
// (by the gas's hue), silver split into a spectrum at its edges, or a slow
// rainbow drifting across the box. Dense gas burns toward white, dust lanes
// absorb, and film grain sits on top.
float dens(vec2 uv) {
  vec3 c = texture(uDye, uv).rgb;
  return max(max(c.r, c.g), c.b);
}

vec3 glow(vec4 dye) {
  float m = max(max(dye.r, dye.g), dye.b);
  vec3 n = dye.rgb / max(m, 1e-4);
  vec3 light;
  if (uGlow > 2.5) {
    // Rainbow: hue sweeps across the box and drifts, bent a little by the gas.
    vec3 mid, high;
    rainbow(uTime * 0.04 + dot(vUv, vec2(0.8, 0.45)) * 3.5 + n.r * 1.2, mid, high);
    float L = pow(m, GLOW_CURVE) * GLOW_GAIN;
    vec3 ink = max(fromOklab(mix(mid, high, smoothstep(0.0, 1.2, L))), 0.0);
    light = ink * L * 1.6;
  } else if (uGlow > 1.5) {
    // Silver prism: the body stays silver; edges split into a spectrum.
    vec2 px = 1.0 / vec2(textureSize(uDye, 0));
    vec2 g = vec2(dens(vUv + vec2(px.x * 2.0, 0.0)) - dens(vUv - vec2(px.x * 2.0, 0.0)),
                  dens(vUv + vec2(0.0, px.y * 2.0)) - dens(vUv - vec2(0.0, px.y * 2.0)));
    vec2 dir = g / (length(g) + 1e-3) * px * PRISM_SPLIT;
    // Seven samples along the gas's gradient, each weighted by its band's
    // color, so edges fan out smoothly from red to blue.
    vec3 sum = vec3(0.0), wsum = vec3(0.0);
    for (int i = 0; i < 7; i++) {
      float t = float(i) / 6.0;
      vec3 w = max(vec3(1.0 - abs(t - 0.15) * 3.0, 1.0 - abs(t - 0.5) * 3.0, 1.0 - abs(t - 0.85) * 3.0), 0.0);
      sum += w * pow(dens(vUv + dir * (t - 0.5) * 2.0), GLOW_CURVE);
      wsum += w;
    }
    light = sum / wsum * GLOW_GAIN * 0.6 * vec3(0.9, 0.88, 0.86);
    float grey = dot(light, vec3(0.3333));
    light = max(grey + (light - grey) * 2.0, 0.0); // let the split read as color
  } else {
    float toMid = smoothstep(0.25, 0.57, n.r);
    float toCore = smoothstep(0.6, 1.0, n.r);
    float hot = smoothstep(0.45, 0.75, n.g) * toCore;
    vec3 ink = mix(mix(uInk[0], uInk[1], toMid), uInk[2], toCore);
    ink = toLinear(mix(ink, uInk[3], hot));
    light = ink * pow(m, GLOW_CURVE) * GLOW_GAIN;
  }
  float burn = smoothstep(GLOW_BURN_LO, GLOW_BURN_HI, m);
  light += burn * burn * GLOW_WHITE;
  light *= 1.0 - dye.a * 0.6;
  return toLinear(uGround) + 1.0 - exp(-light);
}

void main() {
  if (uGlow > 0.5) {
    outColor = filmGrain(glow(texture(uDye, vUv)));
    return;
  }
  if (uPaint > 0.5) {
    vec3 col = paint(texture(uDye, vUv));
    outColor = vec4(col + (hash12(gl_FragCoord.xy + fract(uTime) * 61.7) - 0.5) / 255.0 * 2.0, 1.0);
    return;
  }
  vec3 col = skyColor(vUv.y);
  vec2 suv = vec2(vUv.x * uAspect, vUv.y);

  // Sample the nebula first: local gas density gates the bright-star glow.
  vec4 dye = texture(uDye, vUv);
  float gasDen = clamp(dot(dye.rgb, vec3(0.6)), 0.0, 1.0);

  float stars = 0.0;
  stars += starLayer(suv, 170.0, 0.28) * 0.30;  // dust of tiny stars
  stars += starLayer(suv + vec2(53.0, 0.0), 95.0, 0.12) * 0.55; // far
  stars += starLayer(suv + vec2(37.0, 0.0), 55.0, 0.08) * 0.85; // mid
  vec3 starCol = mix(vec3(0.70, 0.78, 1.0), vec3(1.0), clamp(stars, 0.0, 1.0));
  col += stars * starCol;
  float glow = smoothstep(0.02, 0.28, gasDen);
  col += brightStars(suv + vec2(11.0, 0.0), glow);

  // Nebula: dust lanes absorb, gas emits.
  col *= 1.0 - dye.a * 0.8;              // dark dust dims sky and stars behind it
  vec3 gas = 1.0 - exp(-dye.rgb * 1.7);  // soft additive: hot cores without clipping
  col += gas;

  // Gentle filmic-ish curve + dither to prevent gradient banding.
  col = col / (1.0 + col * 0.15);
  float dither = (hash12(gl_FragCoord.xy + fract(uTime) * 61.7) - 0.5) / 255.0 * 2.0;
  col += dither;

  outColor = vec4(col, 1.0);
}
`;
