// Vendored from wevial/spectra@d34e0f8 backgrounds/ribbons/src/shaders.ts (MIT, see ../LICENSE).
import { NOISE, COLOR, GRAIN } from '../core/glsl';
import { MAX_RIBBONS, MAX_TAPS } from './params';

// The ribbon field, shared by the bloom seed (quarter resolution) and the
// composite (full resolution). Everything is analytic: no geometry, no
// texture, so edges are exact at any size.
//
// Coordinates: each ribbon has a frame (center, angle, scale) in which x
// runs -1..1 between its two pinch points and y is in the same units.
// Centered and full-bleed, every frame is the same; `fill` gives each
// ribbon its own height, tilt and length. A lens-shaped envelope
// e(x) = 1 - x² carries both the centerline and the thickness, so every
// ribbon is thick in the middle and tapers to nothing at its pinch points.
//
// Ribbons sit at different depths: the back ones are softer and dimmer. A
// thin-film sheen shifts the color along and across each ribbon, and now
// and then a glint of light runs along a ribbon from one end to the other.
//
// Distance to a ribbon is the vertical offset from its centerline divided by
// sqrt(1 + slope²): the perpendicular distance to first order, so steep
// stretches keep their width instead of thinning. One pixel in those units
// (uPixel, over the ribbon's scale) sets the antialiasing width of every
// edge. It's computed rather than taken from fwidth so that a pixel can skip
// the ribbons it's nowhere near: most pixels are close to one or two.
const RIBBONS = /* glsl */ `
uniform int uCount;
uniform vec4 uWave[${MAX_RIBBONS}];   // freq 1, phase 1, freq 2, phase 2
uniform vec4 uShape[${MAX_RIBBONS}];  // amplitude, offset, half-thickness, swell phase
uniform vec4 uFrame[${MAX_RIBBONS}];  // center x, y, angle, scale: where each ribbon sits
uniform vec4 uExtra[${MAX_RIBBONS}];  // sheen frequency, sheen phase, depth 0..1, glint position
uniform vec3 uColor[${MAX_RIBBONS}];  // linear
uniform float uFringe;                // chromatic split, fraction of the half-width
uniform float uWhite;                 // how hard overlaps burn to white
uniform float uDepth;                 // how much depth softens and dims the back ribbons
uniform float uSheen;                 // thin-film color shift
uniform float uGlints;                // brightness of the traveling glints
uniform float uPixel;                 // one pixel of the target, in box units

// Cross-section at distance d from the centerline, half-width h, pixel size
// aa, extra edge blur for depth. Soft-edged and brighter toward the rim
// than the middle, like light grazing the edge of a translucent tube. A
// ribbon thinner than a pixel is drawn one pixel wide and dimmed in proportion, so the tapered ends fade
// out instead of breaking into crawling dashes.
float section(float d, float h, float aa, float blur) {
  float he = max(h, aa);
  float soft = max(he * 0.5, aa) + blur;
  float body = 1.0 - smoothstep(he - soft, he + aa + blur, d);
  float s = clamp(d / he, 0.0, 1.0);
  return body * (0.2 + 1.0 * s * s * s) * (h / he);
}

vec3 ribbons(vec2 p) {
  vec3 col = vec3(0.0);
  float sum = 0.0, top = 0.0;
  for (int i = 0; i < ${MAX_RIBBONS}; i++) {
    if (i >= uCount) break;
    // Into this ribbon's own frame: its pinch points at x = ±1.
    vec4 f = uFrame[i];
    vec2 r = p - f.xy;
    float ca = cos(f.z), sa = sin(f.z);
    vec2 q = vec2(ca * r.x + sa * r.y, ca * r.y - sa * r.x) / f.w;
    float x = q.x;
    float e = 1.0 - x * x;
    float de = -2.0 * x;
    float taper = max(e, 0.0);
    vec4 w = uWave[i];
    vec4 s = uShape[i];
    float a1 = w.x * x + w.y, a2 = w.z * x + w.w;
    float wav = s.x * (0.72 * sin(a1) + 0.28 * sin(a2)) + s.y;
    float dwav = s.x * (0.72 * w.x * cos(a1) + 0.28 * w.z * cos(a2));
    float c = e * wav;
    float dc = de * wav + e * dwav;
    float d = (q.y - c) * inversesqrt(1.0 + dc * dc);
    float aa = 1.2 * uPixel / f.w;
    // Thick in the middle, a slow swell running along it.
    float h = s.z * pow(taper, 1.25) * (1.0 + 0.3 * sin(2.3 * x + s.w));
    vec4 g = uExtra[i];
    // Depth: back ribbons get softer edges and less light.
    float z = g.z * uDepth;
    float blur = z * h * 0.9;
    // Nowhere near this ribbon: it adds nothing here.
    if (abs(d) > h * (1.0 + uFringe) + blur + 2.0 * aa) continue;
    float gain = 1.0 - 0.5 * z;
    // A glint running along the ribbon.
    float gx = (x - g.w) / 0.1;
    gain *= 1.0 + uGlints * 2.5 * exp(-gx * gx);
    // Red rides a touch above the centerline and blue a touch below, like
    // light dispersed through a prism: a warm upper edge, a cool lower one,
    // and white again where the channels coincide in the middle.
    float split = uFringe * h;
    vec3 sec = vec3(
      section(abs(d - split), h, aa, blur),
      section(abs(d), h, aa, blur),
      section(abs(d + split), h, aa, blur));
    // Thin-film sheen: the hue rolls slowly along the ribbon and across it.
    float film = 2.0 * (g.x * x + g.y) + 1.5 * d / max(h, aa);
    vec3 tint = 1.0 + uSheen * 0.6 * cos(film + vec3(0.0, 2.094, 4.189));
    col += uColor[i] * tint * sec * gain;
    sum += sec.g;
    top = max(top, sec.g);
  }
  // Where ribbons overlap, light adds toward white.
  float overlap = sum - top;
  return col + vec3(overlap * overlap / (0.45 + overlap)) * uWhite;
}
`;

// Bloom seed: the ribbon field at quarter CSS resolution. A pixel is four
// CSS px here, so the ribbons come out already softened.
export const SEED = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform vec2 uBox;    // box size in ribbon units
uniform vec2 uCenter; // ribbon origin, uv
out vec4 fragColor;
${RIBBONS}

void main() {
  fragColor = vec4(min(ribbons((vUv - uCenter) * uBox), vec3(4.0)), 1.0);
}
`;

// Separable Gaussian, one axis per pass, with linear-sampling taps: each tap
// past the center reads between two texels, weighted for both. See
// gaussianTaps() for the offsets and weights.
export const BLUR = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uSrc;
uniform vec2 uStep;                 // one texel along the blur axis, in uv
uniform int uTaps;
uniform float uOffset[${MAX_TAPS}];
uniform float uWeight[${MAX_TAPS}];
out vec4 fragColor;

void main() {
  vec3 s = texture(uSrc, vUv).rgb * uWeight[0];
  for (int i = 1; i < uTaps; i++) {
    vec2 o = uStep * uOffset[i];
    s += (texture(uSrc, vUv + o).rgb + texture(uSrc, vUv - o).rgb) * uWeight[i];
  }
  fragColor = vec4(s, 1.0);
}
`;

// Full resolution: the ribbons, then two bloom levels (a tight halo and a
// wide soft glow), rolled off toward white, then film grain over pure black.
export const COMPOSITE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform vec2 uBox;
uniform vec2 uCenter;
uniform sampler2D uHalo;    // quarter resolution, blurred
uniform sampler2D uWide;    // eighth resolution, blurred wider
uniform float uGlow;
uniform float uBright;      // breath and mount fade-in
out vec4 fragColor;
${NOISE}
${COLOR}
${GRAIN}
${RIBBONS}

// Highlights roll off past a knee instead of clipping, and what spills over
// one channel bleeds into the others, so hot overlaps go white, not neon.
vec3 shoulder(vec3 c) {
  float over = max(max(c.r, c.g), c.b) - 1.0;
  c += max(over, 0.0) * 0.25;
  const float k = 0.6;
  return mix(c, k + (1.0 - k) * (1.0 - exp(-(c - k) / (1.0 - k))), step(k, c));
}

void main() {
  vec3 col = ribbons((vUv - uCenter) * uBox);
  col += (texture(uHalo, vUv).rgb * 0.3 + texture(uWide, vUv).rgb * 0.55) * uGlow;
  fragColor = filmGrain(shoulder(col * uBright));
}
`;
