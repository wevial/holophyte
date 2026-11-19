// Vendored from wevial/spectra@4d4c754 packages/core/src/glsl.ts (MIT, see ../LICENSE).
// GLSL chunks shared by effects. Interpolate into a shader source with ${...}.

/** hash12, hash32, value noise, and 4-octave fbm. */
export const NOISE = /* glsl */ `
float hash12(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}
vec3 hash32(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
  p3 += dot(p3, p3.yxz + 33.33);
  return fract((p3.xxy + p3.yzz) * p3.zyx);
}
float vnoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash12(i), hash12(i + vec2(1, 0)), u.x),
             mix(hash12(i + vec2(0, 1)), hash12(i + vec2(1, 1)), u.x), u.y);
}
float fbm(vec2 p) {
  float v = 0.0, amp = 0.5;
  mat2 rot = mat2(0.8, 0.6, -0.6, 0.8);
  for (int i = 0; i < 4; i++) { v += amp * vnoise(p); p = rot * p * 2.03; amp *= 0.5; }
  return v;
}
`;

/** sRGB <-> linear, and linear <-> Oklab. */
export const COLOR = /* glsl */ `
vec3 toLinear(vec3 c) {
  return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}
vec3 toSrgb(vec3 c) {
  c = max(c, 0.0);
  return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
vec3 toOklab(vec3 c) {
  vec3 lms = pow(vec3(
    dot(c, vec3(0.4122214708, 0.5363325363, 0.0514459929)),
    dot(c, vec3(0.2119034982, 0.6806995451, 0.1073969566)),
    dot(c, vec3(0.0883024619, 0.2817188376, 0.6299787005))), vec3(1.0 / 3.0));
  return vec3(
    dot(lms, vec3(0.2104542553, 0.7936177850, -0.0040720468)),
    dot(lms, vec3(1.9779984951, -2.4285922050, 0.4505937099)),
    dot(lms, vec3(0.0259040371, 0.7827717662, -0.8086757660)));
}
vec3 fromOklab(vec3 c) {
  vec3 lms = vec3(
    dot(c, vec3(1.0, 0.3963377774, 0.2158037573)),
    dot(c, vec3(1.0, -0.1055613458, -0.0638541728)),
    dot(c, vec3(1.0, -0.0894841775, -1.2914855480)));
  lms = lms * lms * lms;
  return vec3(
    dot(lms, vec3(4.0767416621, -3.3077115913, 0.2309699292)),
    dot(lms, vec3(-1.2684380046, 2.6097574011, -0.3413193965)),
    dot(lms, vec3(-0.0041960863, -0.7034186147, 1.7076147010)));
}
`;

/**
 * smoke(p, t): fbm warped by fbm, drifting slowly with t, so it billows and
 * morphs like smoke. Returns two channels centered on ~0 (roughly ±0.3).
 * Needs NOISE.
 */
export const SMOKE = /* glsl */ `
vec2 smoke(vec2 n, float t) {
  vec2 w = vec2(fbm(n + vec2(0.0, 0.021 * t)), fbm(n + vec2(5.2, 1.3) - vec2(0.017 * t, 0.0)));
  w = vec2(fbm(n + 2.0 * w + vec2(1.7, 9.2) + 0.012 * t), fbm(n + 2.0 * w + vec2(8.3, 2.8) - 0.01 * t));
  return w - 0.47;
}
`;

/**
 * overPage(srgb): the output fragment for a final color drawn over black.
 * Opaque by default. On a transparent stage (`setTransparent`) it is
 * premultiplied with alpha from the light at the pixel, its brightest
 * channel: unlit pixels let the page through, and over a black page the
 * result is exactly `srgb`, so a dark page looks the same and a light one is
 * tinted by the colors. GRAIN includes this chunk. An effect that blends
 * from a ground color sets `uPage` to it: the alpha is then the least that
 * reproduces `srgb` over that color, so the blend keeps its ground.
 */
export const TRANSPARENT = /* glsl */ `
uniform float uTransparent;  // 1 on a transparent stage
uniform vec3 uPage;          // sRGB the color is drawn over; black unless set

vec4 overPage(vec3 srgb) {
  vec3 c = clamp(srgb, 0.0, 1.0);
  if (uTransparent < 0.5) return vec4(c, 1.0);
  vec3 need = max((uPage - c) / max(uPage, 1e-4), (c - uPage) / max(1.0 - uPage, 1e-4));
  float a = clamp(max(max(need.r, need.g), need.b), 0.0, 1.0);
  return vec4(clamp(c - uPage * (1.0 - a), 0.0, a), a);
}
`;

/**
 * Sets TRANSPARENT's uniform on the program in use: on when the context has
 * an alpha channel, which only a `transparent` stage asks for. `setGrain`
 * calls it.
 */
export function setTransparent(gl: WebGL2RenderingContext, uniforms: Record<string, WebGLUniformLocation | null>) {
  gl.uniform1f(uniforms.uTransparent, gl.getContextAttributes()?.alpha ? 1 : 0);
}

/**
 * filmGrain(col): film grain plus output encoding. Linear color in, the
 * final fragment out through `overPage`, so drawn over black. The grain is a
 * per-pixel hash added in linear light, weighted toward the darks; two
 * patterns crossfade (see `setGrain`) so it morphs instead of flickering. A
 * static sub-LSB dither keeps dark gradients from banding even with grain
 * off. On a transparent stage the grain is light like the rest, so it reads
 * on a dark page and vanishes into a light one. Needs NOISE and COLOR.
 */
export const GRAIN = /* glsl */ `
${TRANSPARENT}
uniform float uGrain;       // strength; the shaders FilmGrain scale
uniform vec2 uGrainSeeds;   // the two grain patterns being crossfaded
uniform float uGrainMix;    // 0..1 from the first to the second

vec4 filmGrain(vec3 col) {
  // Dividing by the blend's spread keeps the grain equally strong mid-fade instead of dimming.
  float n0 = hash12(gl_FragCoord.xy + uGrainSeeds.x * vec2(419.3, 257.1)) * 2.0 - 1.0;
  float n1 = hash12(gl_FragCoord.xy + uGrainSeeds.y * vec2(419.3, 257.1)) * 2.0 - 1.0;
  float k = smoothstep(0.0, 1.0, uGrainMix);
  float n = mix(n0, n1, k) / sqrt(k * k + (1.0 - k) * (1.0 - k));
  float l = clamp(dot(col, vec3(0.2126, 0.7152, 0.0722)), 0.0, 1.0);
  col += n * (1.0 - l) * (1.0 - l) * uGrain * 0.1;
  return overPage(toSrgb(col) + (hash12(gl_FragCoord.xy + 71.0) - 0.5) / 255.0);
}
`;

/**
 * Sets GRAIN's uniforms on the program in use, TRANSPARENT's included.
 * `clock` counts grain periods: the pattern crossfades into the next one as
 * it advances by 1.
 */
export function setGrain(gl: WebGL2RenderingContext, uniforms: Record<string, WebGLUniformLocation | null>, strength: number, clock: number) {
  const g = Math.floor(clock);
  gl.uniform1f(uniforms.uGrain, strength);
  gl.uniform2f(uniforms.uGrainSeeds, g % 256, (g + 1) % 256);
  gl.uniform1f(uniforms.uGrainMix, clock - g);
  setTransparent(gl, uniforms);
}

/**
 * rainbow(h, mid, high): two Oklab colors at hue angle h (radians) on an
 * Oklch wheel, the darker `mid` trailing `high` by ~75deg. Sweep h across
 * space and time for a drifting rainbow.
 */
export const RAINBOW = /* glsl */ `
void rainbow(float h, out vec3 mid, out vec3 high) {
  high = vec3(0.74, 0.17 * cos(h), 0.17 * sin(h));
  mid = vec3(0.6, 0.16 * cos(h + 1.3), 0.16 * sin(h + 1.3));
}
`;
