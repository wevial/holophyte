// Vendored from wevial/spectra@256aa26 cursors/mycelium/src/shaders.ts (MIT, see ../LICENSE).
import { NOISE } from '../core/glsl';

// Physarum-style transport network (Jeff Jones, 2010). Agents walk a scent
// field: each senses ahead-left, ahead and ahead-right, turns toward the
// strongest, steps forward and drops scent. The field blurs and evaporates.
// Shared paths reinforce into veins; unused ones fade.
//
// Agents texture (RGBA32F, one texel per agent): xy = position (uv), z = heading.
// Field (R16F, one texel per grid cell): scent.

/** Spores in a small disc near the middle, heading outward, so the colony blooms. */
export const AGENT_INIT = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform float uAspect;
uniform float uSeed;
out vec4 fragColor;
${NOISE}
void main() {
  vec3 r = hash32(gl_FragCoord.xy + uSeed);
  float ang = r.x * 6.2831853;
  float rad = sqrt(r.y) * 0.06;
  vec2 pos = vec2(0.5) + vec2(cos(ang) / uAspect, sin(ang)) * rad;
  fragColor = vec4(pos, ang + (r.z - 0.5) * 0.6, 0.0);
}
`;

/** Sense, turn, step. The cursor trail counts as scent, so veins reroute toward it. */
export const AGENT_STEP = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uAgents;
uniform sampler2D uField;
uniform sampler2D uTrail;
uniform vec2 uCells;        // field size in cells
uniform float uSensorAngle; // rad
uniform float uSensorDist;  // cells
uniform float uTurn;        // rad per step
uniform float uSpeed;       // cells per step
uniform float uAttract;     // scent value of the cursor trail
uniform float uWander;      // random heading jitter per step, rad
uniform float uSeed;
out vec4 fragColor;
${NOISE}

float sense(vec2 pos, float ang) {
  vec2 p = fract(pos + vec2(cos(ang), sin(ang)) * uSensorDist / uCells);
  return texture(uField, p).r + texture(uTrail, p).z * uAttract;
}

void main() {
  vec4 a = texture(uAgents, vUv);
  vec2 pos = a.xy;
  float h = a.z;
  float rnd = hash12(gl_FragCoord.xy * 1.37 + uSeed);

  float f = sense(pos, h);
  float l = sense(pos, h + uSensorAngle);
  float r = sense(pos, h - uSensorAngle);
  float turn = uTurn * (0.6 + 0.8 * rnd);
  if (f > l && f > r) {
    // keep going
  } else if (f < l && f < r) {
    h += (rnd - 0.5) * 2.0 * turn;
  } else if (l > r) {
    h += turn;
  } else if (r > l) {
    h -= turn;
  }

  // The plate wraps, so nothing piles up at the edges.
  h += (hash12(gl_FragCoord.yx * 0.71 + uSeed) - 0.5) * uWander;
  pos = fract(pos + vec2(cos(h), sin(h)) * uSpeed / uCells);
  fragColor = vec4(pos, mod(h, 6.2831853), a.w);
}
`;

/** One point per agent, added into the field. */
export const DEPOSIT_VERT = /* glsl */ `#version 300 es
precision highp float;
layout(location = 0) in float aIndex;
uniform sampler2D uAgents;
uniform int uSide;
void main() {
  int i = int(aIndex);
  vec4 a = texelFetch(uAgents, ivec2(i % uSide, i / uSide), 0);
  gl_Position = vec4(a.xy * 2.0 - 1.0, 0.0, 1.0);
  gl_PointSize = 1.0;
}
`;

export const DEPOSIT_FRAG = /* glsl */ `#version 300 es
precision highp float;
uniform float uDeposit;
out vec4 fragColor;
void main() { fragColor = vec4(uDeposit, 0.0, 0.0, 1.0); }
`;

/** 3x3 blur and evaporation. */
export const DIFFUSE = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uField;
uniform vec2 uTexel;
uniform float uDecay;
out vec4 fragColor;
void main() {
  float sum = 0.0;
  for (int y = -1; y <= 1; y++)
    for (int x = -1; x <= 1; x++)
      sum += texture(uField, vUv + vec2(x, y) * uTexel).r;
  // Capped: a saturated vein stops pulling harder, so agents spread into
  // neighbours and the net stays fine instead of collapsing into a few lanes.
  fragColor = vec4(min(sum / 9.0, 1.0) * uDecay, 0.0, 0.0, 1.0);
}
`;

/** Soil, a faint haze of exploration, bright veins with relief, glowing hubs. */
export const RENDER = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uField;
uniform sampler2D uTrail;
uniform vec2 uTexel;
uniform vec2 uRes;
uniform float uTime;
uniform float uGain;
uniform float uGlow;
uniform vec3 uGround;
uniform vec3 uHaze;
uniform vec3 uHypha;
uniform vec3 uTip;
out vec4 fragColor;
${NOISE}

float density(vec2 uv) { return clamp(texture(uField, uv).r * uGain, 0.0, 1.0); }

void main() {
  float d = density(vUv);
  float core = smoothstep(0.5, 0.95, d);
  vec2 g = vec2(density(vUv + vec2(uTexel.x, 0.0)) - density(vUv - vec2(uTexel.x, 0.0)),
                density(vUv + vec2(0.0, uTexel.y)) - density(vUv - vec2(0.0, uTexel.y)));
  float light = dot(normalize(vec3(-g * 2.0, 1.0)), normalize(vec3(-0.5, 0.6, 0.65)));

  vec2 p = vUv * vec2(uRes.x / uRes.y, 1.0);
  float soil = fbm(p * 5.0) * 0.6 + vnoise(p * 160.0) * 0.4;
  vec3 col = uGround * (0.7 + 0.6 * soil);

  // Fuzz: wandering agents and the blur around each strand.
  col = mix(col, uHaze, smoothstep(0.02, 0.5, d) * 0.7);
  col = mix(col, uHypha * (0.65 + 0.5 * light), core);
  // Strands the cursor fed glow.
  float trail = texture(uTrail, vUv).z;
  col = mix(col, uTip, clamp(trail * uGlow * (0.3 + core), 0.0, 1.0));

  vec2 c = vUv - 0.5;
  col *= 1.0 - dot(c, c) * 0.7;
  col += (hash12(gl_FragCoord.xy + fract(uTime) * 100.0) - 0.5) * 0.03;
  fragColor = vec4(col, 1.0);
}
`;
