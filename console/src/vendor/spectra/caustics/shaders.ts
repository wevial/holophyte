// Vendored from wevial/spectra@4d4c754 backgrounds/caustics/src/shaders.ts (MIT, see ../LICENSE).
import { NOISE, COLOR, GRAIN } from '../core/glsl';

export const WAVES = 9;

// Pass 1, once per wavelength, additive into a float target: the water
// surface as a dense mesh. Each vertex is a point on the surface; sunlight
// falls straight down, refracts through the surface normal there, and lands
// on the floor `uDepth` below. The mesh is drawn where it lands, so where
// the waves focus light, triangles crowd together and overlap.
//
// Brightness is how much the surface around a point shrinks on its way to
// the floor: 1 / det of the mapping's Jacobian, which for small slopes is
// I + c·H with H the surface's Hessian (exact for the summed sines). The det
// is interpolated and inverted per fragment, so the peaks run smoothly
// along the web instead of beading at the mesh spacing, and softened near
// zero so a peak tops out instead of plateauing.
//
// Past the focal depth light folds over itself: that's what makes ribbons
// cross, double up and pinch into little lenses at junctions. Left alone,
// the folds grow into wide flat bands with hard edges, so focusing is
// soft-capped with a tanh on the Hessian's most negative eigenvalue: x = 1
// is a fold, and FOCUS = 1.5 lets the strongest crests overshoot into thin
// doubled lines while flattening the rest. A blur pass after this softens
// what fold edges remain, the way the sun's disk does.
export const LIGHT_VS = /* glsl */ `#version 300 es
precision highp float;
layout(location = 0) in vec2 aGrid;   // 0..1 over the mesh
uniform vec2 uBox;                     // box in units
uniform float uMargin;                 // mesh overhang per side, units
uniform vec4 uWave[${WAVES}];          // kx, ky, amplitude, phase
uniform float uEta;                    // 1 / index of refraction, this wavelength
uniform float uDepth;
out float vDet;

const float FOCUS = 1.5;   // cap on focusing; 1 is a fold

void main() {
  vec2 p = aGrid * (uBox + 2.0 * uMargin) - uMargin;
  float h = 0.0;
  vec2 grad = vec2(0.0);
  vec3 hess = vec3(0.0);  // xx, yy, xy
  for (int i = 0; i < ${WAVES}; i++) {
    vec4 w = uWave[i];
    float a = dot(w.xy, p) + w.w;
    h += w.z * sin(a);
    grad += w.z * cos(a) * w.xy;
    hess -= w.z * sin(a) * vec3(w.x * w.x, w.y * w.y, w.x * w.y);
  }
  // Paraxial refraction: light shifts sideways by c·grad on its way down.
  float c = (uDepth + h) * (1.0 - uEta);
  // The Hessian's most negative eigenvalue: the direction that focuses hardest.
  float mid = 0.5 * (hess.x + hess.y);
  float lo = mid - sqrt(0.25 * (hess.x - hess.y) * (hess.x - hess.y) + hess.z * hess.z);
  float x = max(-c * lo, 1e-4);           // 1 is a fold
  c *= FOCUS * tanh(x / FOCUS) / x;
  vec2 q = p + c * grad;
  vDet = (1.0 + c * hess.x) * (1.0 + c * hess.y) - c * c * hess.z * hess.z;
  gl_Position = vec4(q / uBox * 2.0 - 1.0, 0.0, 1.0);
}
`;

export const LIGHT_FS = /* glsl */ `#version 300 es
precision highp float;
in float vDet;
uniform vec3 uColor;   // this wavelength's share of white light
out vec4 fragColor;

void main() {
  // 1 / |det|, softened so a fold's peak tops out near 8 instead of clipping flat.
  fragColor = vec4(uColor * inversesqrt(vDet * vDet + 0.0144), 1.0);
}
`;

// Separable Gaussian, one axis per pass, with linear-sampling taps: each tap
// past the center reads between two texels, weighted for both. See
// gaussianTaps() for the offsets and weights.
export const MAX_TAPS = 8;
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

// Quarter-resolution seed for the glow: a 4x4 box from four bilinear taps,
// clamped so a single hot line can't dominate the bloom.
export const DOWNSAMPLE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uSrc;
out vec4 fragColor;

void main() {
  vec2 t = 1.0 / vec2(textureSize(uSrc, 0));
  vec3 s = min(texture(uSrc, vUv + vec2(t.x, t.y)).rgb, 4.0) + min(texture(uSrc, vUv + vec2(-t.x, t.y)).rgb, 4.0)
         + min(texture(uSrc, vUv + vec2(t.x, -t.y)).rgb, 4.0) + min(texture(uSrc, vUv + vec2(-t.x, -t.y)).rgb, 4.0);
  fragColor = vec4(s * 0.25, 1.0);
}
`;

// Static floor mottle, sampled densely enough to retain its four octaves.
export const MOTTLE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform vec2 uBox;
uniform vec2 uSeed;
out float fragColor;
${NOISE}
void main() {
  fragColor = fbm(vUv * uBox * 1.7 + uSeed);
}
`;

// Pass 2, full resolution: the floor in shade, mottled, lit by the caustics
// through a contrast curve that thins the bright web and darkens the cells,
// plus a wide soft bloom, rolled off toward white so the line cores burn out
// like sunlight. Then a vignette and film grain.
export const COMPOSITE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uLight;   // blurred to the sun's disk
uniform sampler2D uGlow;    // quarter resolution, widely blurred
uniform sampler2D uMottle;
uniform vec3 uFloor;        // linear RGB
uniform vec3 uSun;          // linear RGB
uniform float uIntensity;
uniform float uSharpness;   // contrast exponent on the light
uniform float uReveal;      // mount fade-in, 0..1
uniform float uFill;        // how much plain light fills the cells
out vec4 fragColor;
${NOISE}
${COLOR}
${GRAIN}

// Highlights roll off past a knee instead of clipping, so a colored light
// still burns to white at its brightest.
vec3 shoulder(vec3 c) {
  const float k = 0.62;
  return mix(c, k + (1.0 - k) * (1.0 - exp(-(c - k) / (1.0 - k))), step(k, c));
}

void main() {
  vec3 light = texture(uLight, vUv).rgb;
  vec3 glow = texture(uGlow, vUv).rgb;

  float m = texture(uMottle, vUv).r;
  vec3 floorCol = uFloor * (0.7 + 0.6 * m);
  vec3 sun = uSun;

  // The light averages 1, brighter along the web. The power thins the lines
  // and sinks the cells; the linear part keeps the cells lit, and the glow
  // blooms around the brightest lines.
  vec3 s = vec3(uSharpness);
  vec3 lit = (pow(light, s) + uFill * light + 0.3 * pow(glow, s)) * uIntensity;
  vec3 col = floorCol + sun * lit * uReveal * (0.85 + 0.3 * m);

  vec2 c = vUv - 0.5;
  col *= 1.0 - dot(c, c) * 0.3;

  fragColor = filmGrain(shoulder(col));
}
`;
