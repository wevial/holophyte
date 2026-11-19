// Vendored from wevial/spectra@4d4c754 backgrounds/lava/src/shaders.ts (MIT, see ../LICENSE).
import { NOISE, COLOR, GRAIN, RAINBOW } from '../core/glsl';

// The lamp is a two-fluid simulation on a grid of square cells (the box is
// NY cells tall). Velocities are in cells per second.
//
//   velocity  RG   cells/s
//   state     RGBA wax fraction φ (0 liquid, 1 wax), heat φT (so the wax's
//             temperature T = heat/φ moves with it, never blurred by the
//             liquid around it), hue vector
//
// Each step: advect velocity and state, add forces (buoyancy, surface
// tension, viscosity), conduct and exchange heat, sharpen the wax's edge,
// then project the velocity to be incompressible.

export const MAX_SPOTS = 6;
export const MAX_SEEDS = 8;

const HEAD = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
layout(location = 0) out vec4 outColor;
uniform vec2 uTexel;
`;

/** Starting lamp: a pool of wax on the bulb and a few blobs on their way. */
export const INIT = HEAD + /* glsl */ `
uniform float uAspect;
uniform float uPool;              // pool depth, box heights
uniform vec4 uSeeds[${MAX_SEEDS}];  // x, y (box heights), radius, temperature
uniform int uSeedCount;
${NOISE}
void main() {
  vec2 p = vec2(vUv.x * uAspect, vUv.y);
  float phi = smoothstep(0.006, -0.006, p.y - uPool - 0.015 * (vnoise(vec2(p.x * 5.0, 1.0)) - 0.5));
  float temp = 0.4;
  for (int i = 0; i < ${MAX_SEEDS}; i++) {
    if (i >= uSeedCount) break;
    vec4 b = uSeeds[i];
    float inside = smoothstep(0.006, -0.006, length(p - b.xy) - b.z);
    temp = mix(temp, b.w, inside);
    phi = max(phi, inside);
  }
  float h = vUv.x * 4.0 + vUv.y * 2.0;
  outColor = vec4(phi, phi * temp, cos(h), sin(h));
}
`;

/** Advect both fields through the same velocity and backtrace. */
export const ADVECT = HEAD + /* glsl */ `
uniform sampler2D uVelocity;
uniform sampler2D uState;
layout(location = 1) out vec4 outVelocity;
uniform float uDt;
void main() {
  vec2 back = vUv - uDt * texture(uVelocity, vUv).xy * uTexel;
  outColor = texture(uState, back);
  outVelocity = texture(uVelocity, back);
}
`;

/**
 * Forces on the velocity: buoyancy (wax lighter than the liquid when warm,
 * heavier when cold), surface tension (continuum surface force: curvature
 * times the edge's slope, pulling wax into rounded shapes and thin necks
 * into drops), and the glass.
 */
/**
 * Viscosity: velocity blends toward its neighbors', much more inside wax
 * than in the liquid. Run several times a step: one pass barely resists
 * stretching, and thick wax resists it strongly, so it rises in thick
 * ribbons instead of thinning into threads.
 */
export const VISCOSITY = HEAD + /* glsl */ `
uniform sampler2D uVelocity;
uniform sampler2D uState;
uniform float uViscWax;     // per-pass blend toward the neighbors' velocity
uniform float uViscLiquid;
void main() {
  vec2 dx = vec2(uTexel.x, 0.0), dy = vec2(0.0, uTexel.y);
  vec2 v = texture(uVelocity, vUv).xy;
  vec2 avg = 0.25 * (texture(uVelocity, vUv + dx).xy + texture(uVelocity, vUv - dx).xy
                   + texture(uVelocity, vUv + dy).xy + texture(uVelocity, vUv - dy).xy);
  v = mix(v, avg, mix(uViscLiquid, uViscWax, texture(uState, vUv).r));
  vec2 edge = min(vUv, 1.0 - vUv) / uTexel;
  if (min(edge.x, edge.y) < 1.0) v = vec2(0.0);
  outColor = vec4(v, 0.0, 1.0);
}
`;

export const FORCES = HEAD + /* glsl */ `
uniform sampler2D uVelocity;
uniform sampler2D uState;
uniform float uDt;
uniform float uGravity;     // cells/s² per unit temperature off neutral
uniform float uTension;
uniform float uDamp;        // 1/s
uniform float uFloorHold;   // cells from the floor where wax rests on the coil
uniform float uLiquidLift;  // cells/s²: liquid's upward acceleration relative to wax near the bulb
uniform float uRelease;     // temperature at which wax on the coil lets go

float phi(vec2 uv) { return texture(uState, uv).r; }
vec2 grad(vec2 uv) {
  return 0.5 * vec2(phi(uv + vec2(uTexel.x, 0.0)) - phi(uv - vec2(uTexel.x, 0.0)),
                    phi(uv + vec2(0.0, uTexel.y)) - phi(uv - vec2(0.0, uTexel.y)));
}
vec2 normal(vec2 g) {
  float l = length(g);
  return l > 1e-3 ? g / l : vec2(0.0);
}

void main() {
  vec2 v = texture(uVelocity, vUv).xy;
  vec4 s = texture(uState, vUv);
  vec2 dx = vec2(uTexel.x, 0.0), dy = vec2(0.0, uTexel.y);

  v.y += uDt * uGravity * (s.g - 0.5 * s.r); // φ(T - 0.5)

  vec2 g = grad(vUv);
  if (dot(g, g) > 1e-6) {
    // Neighbor normals share the center and four diagonals.
    float rt = phi(vUv + dx + dy), rb = phi(vUv + dx - dy);
    float lt = phi(vUv - dx + dy), lb = phi(vUv - dx - dy);
    vec2 nr = normal(0.5 * vec2(phi(vUv + dx + dx) - s.r, rt - rb));
    vec2 nl = normal(0.5 * vec2(s.r - phi(vUv - dx - dx), lt - lb));
    vec2 nt = normal(0.5 * vec2(rt - lt, phi(vUv + dy + dy) - s.r));
    vec2 nb = normal(0.5 * vec2(rb - lb, s.r - phi(vUv - dy - dy)));
    float kappa = -0.5 * ((nr.x - nl.x) + (nt.y - nb.y));
    v += uDt * uTension * kappa * g;
  }

  v /= 1.0 + uDamp * uDt;

  // The coil: wax resting on the floor stays on it, so the pool never lifts
  // off as a sheet with liquid underneath; plumes rise from its top instead.
  // It holds wax until it's hot enough to let go, so heat builds into a
  // mass that rises as a fat slug rather than a thin stream.
  float floorHold = s.r * (1.0 - smoothstep(0.0, uFloorHold, vUv.y / uTexel.y)) * (1.0 - smoothstep(uRelease - 0.15, uRelease, s.g / max(s.r, 0.02)));
  if (v.y > 0.0) v.y *= 1.0 - floorHold; // it may still sink into any gap below
  // Liquid rises relative to wax, clearing gaps under the pool. L(y)(1 - φ)
  // equals -L(y)φ plus a height-only pressure gradient. Omit that gradient:
  // the discrete projection leaves a spurious upflow that can cushion wax.
  v.y -= uDt * uLiquidLift * s.r * (1.0 - smoothstep(0.0, 2.0 * uFloorHold, vUv.y / uTexel.y));

  // The glass: no slip at the walls.
  vec2 edge = min(vUv, 1.0 - vUv) / uTexel;
  if (min(edge.x, edge.y) < 1.0) v = vec2(0.0);
  outColor = vec4(v, 0.0, 1.0);
}
`;

/**
 * Heat, carried as φT so it moves with the wax. Conduction evens out the
 * temperature of neighboring wax, the bulb warms wax on the floor over a few
 * wandering hot spots, and wax cools toward the liquid's temperature (warm
 * low, cool high): slowly inside a blob, faster at its surface, so big blobs
 * carry their heat higher than small ones.
 */
export const HEAT = HEAD + /* glsl */ `
uniform sampler2D uState;
layout(location = 1) out vec4 outNormal;
uniform float uDt;
uniform float uConduct;     // per-step blend toward the neighboring wax's temperature
uniform float uHeat;        // 1/s at a hot spot
uniform float uZone;        // height the bulb warms, 0..1
uniform vec2 uSpots[${MAX_SPOTS}]; // x (0..1), strength
uniform float uSpotW;       // spot half-width, 0..1 of the width
uniform float uBaseHeat;    // the bulb's warmth between the spots
uniform float uCoolWax;     // 1/s, through the bulk
uniform float uCoolSurface; // 1/s more where the wax meets the liquid
uniform float uAmbient;     // liquid temperature at the floor; 0 at the top

void main() {
  vec4 s = texture(uState, vUv);
  vec2 dx = vec2(uTexel.x, 0.0), dy = vec2(0.0, uTexel.y);
  vec4 r = texture(uState, vUv + dx), l = texture(uState, vUv - dx);
  vec4 t = texture(uState, vUv + dy), b = texture(uState, vUv - dy);
  float phi = s.r;
  float near = r.r + l.r + t.r + b.r;
  float tw = phi > 1e-3 ? s.g / phi : 0.0;
  float tn = near > 1e-3 ? (r.g + l.g + t.g + b.g) / near : tw;
  tw = mix(tw, tn, uConduct);

  float bulb = uBaseHeat;
  for (int k = 0; k < ${MAX_SPOTS}; k++) {
    float d = (vUv.x - uSpots[k].x) / uSpotW;
    bulb += uSpots[k].y * exp(-d * d);
  }
  float zone = 1.0 - smoothstep(0.0, uZone, vUv.y);
  tw += uDt * uHeat * zone * bulb * (1.0 - tw);

  float exposure = clamp(1.0 - 0.25 * near, 0.0, 1.0);
  float ambient = uAmbient * (1.0 - vUv.y);
  tw += (ambient - tw) * (1.0 - exp(-uDt * (uCoolWax + uCoolSurface * exposure)));

  outColor = vec4(phi, phi * clamp(tw, 0.0, 1.0), s.ba);

  // Heat leaves φ alone, so its neighbors also give sharpening's Sobel
  // normal. Keep its own threshold; surface tension uses a different normal.
  float rb = texture(uState, vUv + vec2(uTexel.x, -uTexel.y)).r;
  float rt = texture(uState, vUv + uTexel).r;
  float lb = texture(uState, vUv - uTexel).r;
  float lt = texture(uState, vUv + vec2(-uTexel.x, uTexel.y)).r;
  vec2 g = vec2((rb + 2.0 * r.r + rt) - (lb + 2.0 * l.r + lt),
                (lt + 2.0 * t.r + rt) - (lb + 2.0 * b.r + rb)) / 8.0;
  float len = length(g);
  outNormal = vec4(len > 0.03 ? g / len : vec2(0.0), 0.0, 1.0);
}
`;

/**
 * Sharpening, part 2: φ -= dτ ∇·F with F = φ(1-φ)n - ε∇φ on the faces
 * between cells, so the edge stays a couple of cells wide and no wax is made
 * or lost. No flux through the walls.
 */
export const SHARPEN = HEAD + /* glsl */ `
uniform sampler2D uState;
uniform sampler2D uFlux;    // cell normals
uniform float uEps;         // edge width, cells
uniform float uStrength;    // pseudo-time step dτ
uniform float uPress;       // compression strength (1)
uniform sampler2D uMass;    // φ with mipmaps: its last level is the lamp's mean
uniform float uMassLod;
uniform float uTarget;      // the mean φ the lamp should hold
uniform float uHold;        // how hard the edges grow or shrink to hold it
// Flux from the a side to the b side. The compression moves wax only in
// proportion to what the giving cell has and the room the taking cell has,
// so no cell ever overfills and the clamp below never throws wax away.
float face(float a, float b, float n) {
  float press = uPress * (n > 0.0 ? n * a * (1.0 - b) : n * b * (1.0 - a));
  return press - uEps * (b - a);
}
// The heat that wax carries across a face: at the giving cell's temperature.
float temp(vec4 s) { return s.r > 1e-4 ? s.g / s.r : 0.0; }
float carried(float f, vec4 a, vec4 b) { return f * (f > 0.0 ? temp(a) : temp(b)); }
void main() {
  vec4 s = texture(uState, vUv);
  vec2 dx = vec2(uTexel.x, 0.0), dy = vec2(0.0, uTexel.y);
  vec2 n = texture(uFlux, vUv).xy;
  vec4 R = texture(uState, vUv + dx), L = texture(uState, vUv - dx);
  vec4 T = texture(uState, vUv + dy), B = texture(uState, vUv - dy);
  float c = s.r;
  float fr = vUv.x + uTexel.x > 1.0 ? 0.0 : face(c, R.r, 0.5 * (n.x + texture(uFlux, vUv + dx).x));
  float fl = vUv.x - uTexel.x < 0.0 ? 0.0 : face(L.r, c, 0.5 * (n.x + texture(uFlux, vUv - dx).x));
  float ft = vUv.y + uTexel.y > 1.0 ? 0.0 : face(c, T.r, 0.5 * (n.y + texture(uFlux, vUv + dy).y));
  float fb = vUv.y - uTexel.y < 0.0 ? 0.0 : face(B.r, c, 0.5 * (n.y + texture(uFlux, vUv - dy).y));
  c -= uStrength * (fr - fl + ft - fb);
  // Heat moves with the wax, so sharpening never strands it in the liquid.
  float h = s.g - uStrength * (carried(fr, s, R) - carried(fl, L, s) + carried(ft, s, T) - carried(fb, B, s));
  // Advection loses (or gains) a little wax at sharp edges; growing or
  // shrinking real edges a touch holds the total without changing shapes.
  // Only where there is an edge, so faint traces never grow into specks.
  float mean = textureLod(uMass, vec2(0.5), uMassLod).r;
  float edge = step(0.5, dot(n, n));
  float grow = edge * uHold * (uTarget - mean) / max(uTarget, 1e-3) * 4.0 * c * (1.0 - c);
  h += grow * temp(s);
  c = clamp(c + grow, 0.0, 1.0);
  outColor = vec4(c, clamp(h, 0.0, c), s.ba);
}
`;

/** Copy φ into the mass texture, whose mipmaps average it. */
export const MASS = HEAD + /* glsl */ `
uniform sampler2D uState;
void main() { outColor = vec4(texture(uState, vUv).r, 0.0, 0.0, 1.0); }
`;

export const DIVERGENCE = HEAD + /* glsl */ `
uniform sampler2D uVelocity;
void main() {
  vec2 C = texture(uVelocity, vUv).xy;
  float L = texture(uVelocity, vUv - vec2(uTexel.x, 0.0)).x;
  float R = texture(uVelocity, vUv + vec2(uTexel.x, 0.0)).x;
  float B = texture(uVelocity, vUv - vec2(0.0, uTexel.y)).y;
  float T = texture(uVelocity, vUv + vec2(0.0, uTexel.y)).y;
  if (vUv.x - uTexel.x < 0.0) L = -C.x;
  if (vUv.x + uTexel.x > 1.0) R = -C.x;
  if (vUv.y - uTexel.y < 0.0) B = -C.y;
  if (vUv.y + uTexel.y > 1.0) T = -C.y;
  outColor = vec4(0.5 * (R - L + T - B), 0.0, 0.0, 1.0);
}
`;

export const PRESSURE = HEAD + /* glsl */ `
uniform sampler2D uPressure;
uniform sampler2D uDivergence;
void main() {
  float L = texture(uPressure, vUv - vec2(uTexel.x, 0.0)).x;
  float R = texture(uPressure, vUv + vec2(uTexel.x, 0.0)).x;
  float B = texture(uPressure, vUv - vec2(0.0, uTexel.y)).x;
  float T = texture(uPressure, vUv + vec2(0.0, uTexel.y)).x;
  outColor = vec4((L + R + B + T - texture(uDivergence, vUv).x) * 0.25, 0.0, 0.0, 1.0);
}
`;

export const SUBTRACT = HEAD + /* glsl */ `
uniform sampler2D uPressure;
uniform sampler2D uVelocity;
void main() {
  float L = texture(uPressure, vUv - vec2(uTexel.x, 0.0)).x;
  float R = texture(uPressure, vUv + vec2(uTexel.x, 0.0)).x;
  float B = texture(uPressure, vUv - vec2(0.0, uTexel.y)).x;
  float T = texture(uPressure, vUv + vec2(0.0, uTexel.y)).x;
  vec2 v = texture(uVelocity, vUv).xy - 0.5 * vec2(R - L, T - B);
  vec2 edge = min(vUv, 1.0 - vUv) / uTexel;
  if (min(edge.x, edge.y) < 1.0) v = vec2(0.0);
  outColor = vec4(v, 0.0, 1.0);
}
`;

/** One axis of a Gaussian blur of the wax fraction: the soft body for shading and glow. */
export const BLUR = HEAD + /* glsl */ `
uniform sampler2D uSource;
uniform vec2 uDir;          // one texel along the blur axis
void main() {
  float w[5] = float[](0.2270, 0.1945, 0.1216, 0.0541, 0.0162);
  float sum = texture(uSource, vUv).r * w[0];
  for (int i = 1; i < 5; i++) {
    vec2 o = uDir * float(i) * 1.5;
    sum += (texture(uSource, vUv + o).r + texture(uSource, vUv - o).r) * w[i];
  }
  outColor = vec4(sum, 0.0, 0.0, 1.0);
}
`;

/**
 * Full resolution. Wax is where φ passes 0.5, sampled bicubically so the
 * outline stays smooth between cells. The blurred body gives a rounded
 * normal (rim light, highlight) and the glow the wax throws into the liquid;
 * temperature brightens warm wax.
 */
export const RENDER = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 fragColor;
uniform sampler2D uState;
uniform sampler2D uBody;
uniform vec2 uSimSize;      // sim grid, cells
uniform vec3 uLiquid;       // sRGB
uniform vec3 uDeep;
uniform vec3 uHot;
uniform float uGlow;
uniform float uReveal;
uniform float uMode;         // 0 palette, 1 rainbow, 2 thermal
uniform float uHue;
${NOISE}
${COLOR}
${GRAIN}
${RAINBOW}

// Cubic B-spline sampling from four bilinear taps.
vec4 bicubic(sampler2D tex, vec2 uv) {
  vec2 p = uv * uSimSize - 0.5;
  vec2 i = floor(p);
  vec2 f = p - i;
  vec2 f2 = f * f, f3 = f2 * f;
  vec2 w0 = (1.0 - 3.0 * f + 3.0 * f2 - f3) / 6.0;
  vec2 w1 = (4.0 - 6.0 * f2 + 3.0 * f3) / 6.0;
  vec2 w2 = (1.0 + 3.0 * f + 3.0 * f2 - 3.0 * f3) / 6.0;
  vec2 w3 = f3 / 6.0;
  vec2 g0 = w0 + w1, g1 = w2 + w3;
  vec2 h0 = (i - 0.5 + w1 / g0) / uSimSize;
  vec2 h1 = (i + 1.5 + w3 / g1) / uSimSize;
  return g0.y * (g0.x * texture(tex, h0) + g1.x * texture(tex, vec2(h1.x, h0.y)))
       + g1.y * (g0.x * texture(tex, vec2(h0.x, h1.y)) + g1.x * texture(tex, h1));
}

const float TH = 0.5;

// Thermal ramp, Oklab: cold indigo through magenta and red to hot gold.
vec3 thermal(float t) {
  const vec3 C0 = vec3(0.28, 0.060, -0.160);  // indigo
  const vec3 C1 = vec3(0.45, 0.170, -0.120);  // violet-magenta
  const vec3 C2 = vec3(0.58, 0.200, 0.080);   // red
  const vec3 C3 = vec3(0.74, 0.090, 0.140);   // orange
  const vec3 C4 = vec3(0.90, 0.010, 0.150);   // gold
  t = clamp(t, 0.0, 1.0) * 4.0;
  if (t < 1.0) return mix(C0, C1, t);
  if (t < 2.0) return mix(C1, C2, t - 1.0);
  if (t < 3.0) return mix(C2, C3, t - 2.0);
  return mix(C3, C4, t - 3.0);
}

void main() {
  vec4 s = bicubic(uState, vUv);
  float f = s.r;
  float warm = clamp(s.g / max(s.r, 0.05), 0.0, 1.0);

  vec3 deep = toOklab(toLinear(uDeep));
  vec3 hot = toOklab(toLinear(uHot));
  vec3 glowDeep = deep, glowHot = hot;
  if (uMode > 1.5) {
    // Color is temperature: deep takes the wax's own, the lit edge a step hotter.
    float t = (warm - 0.05) / 0.95;
    deep = thermal(t) * vec3(0.8, 1.0, 1.0);
    hot = thermal(t + 0.2);
    glowDeep = thermal(0.6) * vec3(0.8, 1.0, 1.0);
    glowHot = thermal(0.85);
    float w = smoothstep(0.1, 0.6, f);
    glowDeep = mix(glowDeep, deep, w);
    glowHot = mix(glowHot, hot, w);
  } else if (uMode > 0.5) {
    // Wax carries its own hue; the liquid's glow takes a smooth sweep, since
    // the hue left in open liquid is only noise.
    float h = (dot(s.ba, s.ba) > 1e-4 ? atan(s.a, s.b) : 0.0) + uHue;
    rainbow(h, deep, hot);
    rainbow(uHue + vUv.x * 2.0, glowDeep, glowHot);
    float w = smoothstep(0.1, 0.6, f);
    glowDeep = mix(glowDeep, deep, w);
    glowHot = mix(glowHot, hot, w);
  }

  float body = texture(uBody, vUv).r;

  vec3 liquid = toLinear(uLiquid);
  float bulb = exp(-pow((vUv.x - 0.5) * 1.4, 2.0) - vUv.y * 3.0);
  vec3 col = liquid + max(fromOklab(glowHot), 0.0) * bulb * 0.12 * uGlow;
  col += max(fromOklab(mix(glowDeep, glowHot, 0.4)), 0.0) * body * body * (0.11 + 0.12 * warm) * uGlow;

  // Compute derivatives before branching: liquid pixels never use the wax
  // normal or highlight, including their four extra body samples.
  float aa = fwidth(f) * 0.8 + 1e-4;
  float inside = smoothstep(TH - aa, TH + aa, f);
  if (inside > 0.0) {
    // Explicit LOD keeps sampling defined in divergent edge quads.
    vec2 e = 1.0 / uSimSize;
    vec2 g = vec2(textureLod(uBody, vUv + vec2(e.x, 0.0), 0.0).r - textureLod(uBody, vUv - vec2(e.x, 0.0), 0.0).r,
                  textureLod(uBody, vUv + vec2(0.0, e.y), 0.0).r - textureLod(uBody, vUv - vec2(0.0, e.y), 0.0).r);
    vec3 n = normalize(vec3(-g * 2.2, 1.0));
    float rim = pow(1.0 - n.z, 1.2);
    float low = clamp(-n.y * 0.5 + (1.0 - vUv.y) * 0.3 + warm * 0.5, 0.0, 1.0);
    vec3 wax = max(fromOklab(mix(deep, hot, clamp(rim * 0.9 + low * 0.6, 0.0, 1.0))), 0.0);
    wax *= 0.7 + 0.4 * (1.0 - vUv.y) + 0.3 * warm;
    float spec = pow(max(dot(n, normalize(vec3(-0.35, 0.55, 1.0))), 0.0), 30.0);
    wax += spec * 0.12;
    col = mix(col, wax, inside);
  }
  col = mix(liquid, col, uReveal);

  vec2 c = vUv - 0.5;
  col *= 1.0 - dot(c, c) * 0.35;
  fragColor = filmGrain(col);
}
`;
