// Vendored from wevial/spectra@d34e0f8 cursors/nebula/src/fluid.js (MIT, see ../LICENSE), given a motion option and a texture size capped to the GPU and the drawing buffer.
// GPU fluid + displacement-field nebula. The velocity sim carries cursor
// momentum; the offset field accumulates it as a local bend of the cloud and
// decays back to zero. The cloud itself is recomputed from the procedural
// base every frame through the bent coordinates, so colors never mix and the
// nebula always heals to its untouched shape.

import { Program, createFBO, createDoubleFBO, makeBlit } from '../core/gl';
import {
  VERTEX, ADVECTION, OFFSET_UPDATE, CLOUD, SPLAT,
  DIVERGENCE, PRESSURE, GRADIENT_SUBTRACT,
} from './shaders.js';

export const CONFIG = {
  simResolution: 224,        // velocity/pressure grid (short edge)
  offsetResolution: 512,     // displacement field (short edge)
  cloudResolution: 1024,     // rendered nebula texture (short edge)
  pressureIterations: 24,
  velocityDissipation: 1.4,  // 1/s — cursor momentum settles quickly
  offsetDecay: 0.9,          // 1/s — the bent cloud closes back in ~1s
  splatForce: 2600,          // pointer velocity -> sim velocity multiplier
  speed: 1,                  // drift speed of the cloud
  currents: 0,               // ambient wanderers stirring the gas (0 = none)
  look: 'space',             // 'space': stars and glowing gas; 'paint': pigment on a flat ground
  paint: 'nebula',           // the paint look's palette (see PAINTS); changing it resets ground
  ground: '#efe8dc',         // the paint look's ground color
  motion: true,              // false freezes the cloud on one frame, as reduced motion does
};

export class FluidSim {
  constructor(gl, tier) {
    this.gl = gl;
    this.config = { ...CONFIG };
    if (tier) {
      this.config.simResolution = tier.sim;
      this.config.cloudResolution = tier.cloud;
      this.config.pressureIterations = tier.iters;
    }
    this.blit = makeBlit(gl);
    this.time = 0;

    this.programs = {
      advection: new Program(gl, VERTEX, ADVECTION),
      offsetUpdate: new Program(gl, VERTEX, OFFSET_UPDATE),
      cloud: new Program(gl, VERTEX, CLOUD),
      splat: new Program(gl, VERTEX, SPLAT),
      divergence: new Program(gl, VERTEX, DIVERGENCE),
      pressure: new Program(gl, VERTEX, PRESSURE),
      gradientSubtract: new Program(gl, VERTEX, GRADIENT_SUBTRACT),
    };

    this.resize();
  }

  resolutionFor(shortEdge) {
    const gl = this.gl;
    const aspect = gl.drawingBufferWidth / gl.drawingBufferHeight;
    let w, h;
    if (aspect >= 1) {
      h = shortEdge;
      w = Math.round(shortEdge * aspect);
    } else {
      w = shortEdge;
      h = Math.round(shortEdge / aspect);
    }
    // A side past the GPU's limit fails to allocate, and detail past the drawing buffer's is never shown.
    const limit = Math.min(gl.getParameter(gl.MAX_TEXTURE_SIZE), Math.max(gl.drawingBufferWidth, gl.drawingBufferHeight, shortEdge));
    const fit = Math.min(1, limit / Math.max(w, h));
    return { w: Math.max(Math.round(w * fit), 8), h: Math.max(Math.round(h * fit), 8) };
  }

  resize() {
    const gl = this.gl;
    const sim = this.resolutionFor(this.config.simResolution);
    const off = this.resolutionFor(this.config.offsetResolution);
    const cloudRes = this.resolutionFor(this.config.cloudResolution);
    this.aspect = gl.drawingBufferWidth / gl.drawingBufferHeight;

    this.velocity?.dispose();
    this.pressure?.dispose();
    this.divergence?.dispose();
    this.offset?.dispose();
    this.cloud?.dispose();

    this.velocity = createDoubleFBO(gl, sim.w, sim.h, gl.RG16F, gl.RG, gl.HALF_FLOAT, gl.LINEAR);
    this.pressure = createDoubleFBO(gl, sim.w, sim.h, gl.R16F, gl.RED, gl.HALF_FLOAT, gl.NEAREST);
    this.divergence = createFBO(gl, sim.w, sim.h, gl.R16F, gl.RED, gl.HALF_FLOAT, gl.NEAREST);
    this.offset = createDoubleFBO(gl, off.w, off.h, gl.RG16F, gl.RG, gl.HALF_FLOAT, gl.LINEAR);
    this.cloud = createFBO(gl, cloudRes.w, cloudRes.h, gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
    this.cloudDirty = true; // freshly-created cloud FBO is transparent black
  }

  // Inject force at a point. field: 'velocity' (directional momentum) or
  // 'offset' (direct local displacement, bypassing the fluid solve).
  // radial 1 pushes away from the point; radial 0 pushes along (dx, dy).
  splat(field, x, y, dx, dy, radius, radial) {
    const gl = this.gl;
    const target = field === 'offset' ? this.offset : this.velocity;
    const p = this.programs.splat;
    p.use();
    gl.uniform1i(p.uniforms.uTarget, target.read.attach(0));
    gl.uniform1f(p.uniforms.uAspect, this.aspect);
    gl.uniform2f(p.uniforms.uPoint, x, y);
    gl.uniform2f(p.uniforms.uDir, dx, dy);
    gl.uniform1f(p.uniforms.uRadius, radius);
    gl.uniform1f(p.uniforms.uRadial, radial);
    this.blit(target.write);
    target.swap();
  }

  step(dt) {
    const gl = this.gl;
    const { programs: pr, velocity, pressure, divergence, offset, blit, config } = this;
    this.time += dt * config.speed;

    gl.disable(gl.BLEND);

    // Pressure projection (incompressible flow = natural swirl-around).
    pr.divergence.use();
    gl.uniform1i(pr.divergence.uniforms.uVelocity, velocity.read.attach(0));
    gl.uniform2f(pr.divergence.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
    blit(divergence);

    pr.pressure.use();
    gl.uniform1i(pr.pressure.uniforms.uDivergence, divergence.attach(1));
    gl.uniform2f(pr.pressure.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
    for (let i = 0; i < config.pressureIterations; i++) {
      gl.uniform1i(pr.pressure.uniforms.uPressure, pressure.read.attach(0));
      blit(pressure.write);
      pressure.swap();
    }

    pr.gradientSubtract.use();
    gl.uniform1i(pr.gradientSubtract.uniforms.uPressure, pressure.read.attach(0));
    gl.uniform1i(pr.gradientSubtract.uniforms.uVelocity, velocity.read.attach(1));
    gl.uniform2f(pr.gradientSubtract.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
    blit(velocity.write);
    velocity.swap();

    // Advect velocity through itself.
    pr.advection.use();
    gl.uniform1i(pr.advection.uniforms.uVelocity, velocity.read.attach(0));
    gl.uniform1i(pr.advection.uniforms.uSource, velocity.read.attach(0));
    gl.uniform2f(pr.advection.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1f(pr.advection.uniforms.uDt, dt);
    gl.uniform1f(pr.advection.uniforms.uDissipation, config.velocityDissipation);
    blit(velocity.write);
    velocity.swap();

    // Accumulate displacement, decaying toward the untouched cloud.
    pr.offsetUpdate.use();
    gl.uniform1i(pr.offsetUpdate.uniforms.uVelocity, velocity.read.attach(0));
    gl.uniform1i(pr.offsetUpdate.uniforms.uOffset, offset.read.attach(1));
    gl.uniform2f(pr.offsetUpdate.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1f(pr.offsetUpdate.uniforms.uDt, dt);
    gl.uniform1f(pr.offsetUpdate.uniforms.uDecay, config.offsetDecay);
    blit(offset.write);
    offset.swap();

    this.renderCloud();
  }

  // Render the cloud through the bent coordinates. Separate from step() so a
  // reduced-motion page can keep a frozen cloud without running the sim.
  renderCloud() {
    const gl = this.gl;
    const p = this.programs.cloud;
    gl.disable(gl.BLEND);
    p.use();
    gl.uniform1i(p.uniforms.uOffset, this.offset.read.attach(0));
    gl.uniform2f(p.uniforms.uOffTexel, this.offset.texelSizeX, this.offset.texelSizeY);
    gl.uniform1f(p.uniforms.uAspect, this.aspect);
    gl.uniform1f(p.uniforms.uTime, this.time);
    this.blit(this.cloud);
    this.cloudDirty = false;
  }
}
