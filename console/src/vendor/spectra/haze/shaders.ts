// Vendored from wevial/spectra@4d4c754 backgrounds/haze/src/shaders.ts (MIT, see ../LICENSE), given a transparent output whose alpha is the smoke's coverage.
import { NOISE, COLOR, SMOKE, GRAIN, RAINBOW } from '../core/glsl';

// Pass 1, half resolution: coverage of the two smoke bands in r and g, and
// the smoke noise in b (the rainbow palette bends its hues along it).
//
// Ported from the AgentMail console header, which is a `shaders` (three.js)
// stack: two soft sine-wave bands (SineWave) under a sine displacement
// (WaveDistortion, mirrored at the edges). On top of that displacement this
// adds domain-warped fbm, scaled by uWarp, so the bands billow like smoke
// instead of only bending.
//
// Lengths are in "units": the box height, capped at a quarter of its longer
// side so a full screen doesn't become one enormous band. A short wide header
// therefore matches the original's proportions.
export const GLOW = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform vec2 uBox;      // box size in units
uniform float uTime;
uniform float uWarp;    // smokiness
uniform vec2 uSeed;     // noise domain offset
uniform vec3 uPhase;    // phase offsets: band a, band b, distortion
out vec4 fragColor;
${NOISE}
${SMOKE}

const float TAU = 6.28318530718;

// SineWave: a band of width 'thick' along a sine, rotated by 'deg', with
// smoothstep edges 'soft' wide. p and pos in units, origin bottom-left.
float band(vec2 p, vec2 pos, float deg, float amp, float freq, float speed, float thick, float soft, float phase) {
  vec2 a = p - pos;
  float r = radians(deg);
  float c = cos(r), s = sin(r);
  float along = a.x * c - a.y * s;
  float across = a.x * s + a.y * c;
  float h = sin(along * freq * TAU + uTime * speed + phase) * amp;
  float m = thick * 0.5, f = soft * 0.5;
  return 1.0 - smoothstep(m - f, m + f, abs(across - h));
}

vec2 mirror(vec2 x) {
  return 1.0 - abs(mod(x, 2.0) - 1.0);
}

void main() {
  vec2 q = (vUv - 0.5) * uBox;

  // WaveDistortion: displace along 140deg by a sine running across it.
  const float D = radians(140.0);
  float ph = (dot(q, vec2(sin(D), cos(D))) + 0.5) * 1.5 * TAU + uTime * 0.1 + uPhase.z;
  vec2 disp = vec2(cos(D), sin(D)) * sin(ph) * 0.2;

  // Smoke, drifting slowly so the bands morph.
  vec2 w = smoke(q * 1.3 + uSeed, uTime);
  disp += w * uWarp * 0.7;

  vec2 p = mirror(vUv + disp / uBox) * uBox;
  float a = band(p, vec2(0.35, 0.6) * uBox, 68.0, 0.5, 0.32, 0.25, 0.9, 0.45, uPhase.x);
  float b = band(p, vec2(0.7, 0.38) * uBox, 152.0, 0.26, 0.5, 0.4, 0.55, 0.6, uPhase.y);
  fragColor = vec4(a, b, w.x, 1.0);
}
`;

// Pass 2, full resolution: band b then band a laid over the base in Oklab,
// eased edge fades back toward the base, a slight vignette, then film grain
// (GRAIN from @spectra/core).
export const COMPOSITE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uGlow;
uniform vec3 uBase;         // sRGB
uniform vec3 uMid;
uniform vec3 uHigh;
uniform float uIntensity;
uniform float uReveal;      // mount fade-in, 0..1
uniform float uFadeBottom;  // fraction of height
uniform float uFadeSide;    // -1..1: sign picks the side, size the strength at that edge
uniform float uRainbow;     // 1: band colors from a drifting hue instead of uMid/uHigh
uniform float uHue;         // rainbow hue drift, radians
out vec4 fragColor;
${NOISE}
${COLOR}
${GRAIN}
${RAINBOW}

void main() {
  vec2 uv = vUv;
  vec3 glow = texture(uGlow, uv).rgb;
  vec2 g = clamp(glow.rg * uIntensity * uReveal, 0.0, 1.0);

  vec3 base = toOklab(toLinear(uBase));
  vec3 mid = toOklab(toLinear(uMid));
  vec3 high = toOklab(toLinear(uHigh));
  if (uRainbow > 0.5) {
    // Oklch hues sweeping diagonally across the box, drifting, and bent by
    // the smoke so color runs along the wisps. The back band trails by ~75deg.
    rainbow(uHue + dot(uv, vec2(0.8, 0.45)) * 3.5 + glow.b * 1.4, mid, high);
  }
  vec3 lab = mix(base, mid, g.y);
  lab = mix(lab, high, g.x);

  // Eased fades back to the base, shaped like the original's DOM scrims.
  float keep = 1.0;
  if (uFadeBottom > 0.0) keep *= 1.0 - pow(1.0 - clamp(uv.y / uFadeBottom, 0.0, 1.0), 3.5);
  float side = uFadeSide < 0.0 ? uv.x : 1.0 - uv.x;
  keep *= 1.0 - abs(uFadeSide) * pow(1.0 - side, 1.6);
  vec3 col = fromOklab(mix(base, lab, keep));

  vec2 c = uv - 0.5;
  col *= 1.0 - dot(c, c) * 0.25;

  fragColor = filmGrain(col);
  // Transparent: the smoke's coverage is the alpha, and the base it blends from is taken out,
  // so a page of the base's color shows the opaque frame and any other page shows through unlit pixels.
  float cover = (1.0 - (1.0 - g.x) * (1.0 - g.y)) * keep;
  if (uTransparent > 0.5) fragColor = vec4(clamp(fragColor.rgb - uBase * (1.0 - cover), 0.0, cover), cover);
}
`;
