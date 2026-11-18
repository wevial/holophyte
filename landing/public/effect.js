(()=>{var{defineProperty:B,getOwnPropertyNames:ne,getOwnPropertyDescriptor:oe}=Object,ie=Object.prototype.hasOwnProperty;function ae(e){return this[e]}var se=(e)=>{var r=(I??=new WeakMap).get(e),t;if(r)return r;if(r=B({},"__esModule",{value:!0}),e&&typeof e==="object"||typeof e==="function"){for(var i of ne(e))if(!ie.call(r,i))B(r,i,{get:ae.bind(e,i),enumerable:!(t=oe(e,i))||t.enumerable})}return I.set(e,r),r},I;var ce=(e)=>e;function ue(e,r){this[e]=ce.bind(null,r)}var de=(e,r)=>{for(var t in r)B(e,t,{get:r[t],enumerable:!0,configurable:!0,set:ue.bind(r,t)})};var be={};de(be,{mountHeader:()=>re});var O=[1,0.85,0.7,0.6,0.5];class k{level=0;budget;settle=0;gpu=-1;frame=-1;refresh=1/0;over=0;under=0;blocked=new Map;constructor({budgetMs:e=6}={}){this.budget=e}get scale(){return O[this.level]}sample(e,r){if(!(e>0)||e>250)return!1;for(let[t,i]of this.blocked)if(i<=e)this.blocked.delete(t);else this.blocked.set(t,i-e);if(this.refresh=Math.min(this.refresh,Math.max(e,6)),this.settle>0)return this.settle-=e,!1;if(r!=null)return this.gpu=this.gpu<0?r:this.gpu+(r-this.gpu)*0.1,this.byGpu(e);return this.frame=this.frame<0?e:this.frame+(e-this.frame)*0.1,this.byFrame(e)}byGpu(e){let r=(t)=>this.gpu*(O[t]/this.scale)**2;if(this.gpu>this.budget){if(this.over+=e,this.under=0,this.over>1000){let t=this.level;while(t<O.length-1&&r(t)>this.budget)t++;return this.set(t)}}else if(this.level>0&&r(this.level-1)<this.budget*0.8){if(this.under+=e,this.over=0,this.under>3000)return this.set(this.level-1)}else this.over=this.under=0;return!1}byFrame(e){if(this.frame>Math.max(this.refresh*1.5,20)||this.frame>45){if(this.under=0,this.over+=e,this.over>1500&&this.level<O.length-1)return this.blocked.set(this.level,30000),this.set(this.level+1)}else if(this.frame<this.refresh*1.15&&this.level>0&&!this.blocked.has(this.level-1)){if(this.over=0,this.under+=e,this.under>6000)return this.set(this.level-1)}else this.over=this.under=0;return!1}set(e){if(this.over=this.under=0,e===this.level)return!1;return this.level=e,this.settle=1000,this.gpu=this.frame=-1,!0}}var w=new WeakMap;function H(e){w.get(e)?.forEach((r)=>r.destroy())}function W(e,r={}){let t=document.createElement("canvas");t.style.cssText="position:absolute;inset:0;width:100%;height:100%;display:block;",e.appendChild(t);let i=t.getContext("webgl2",{alpha:!1,depth:!1,stencil:!1,antialias:!1,powerPreference:"high-performance"});if(!i||r.floatTargets&&!i.getExtension("EXT_color_buffer_float"))return t.remove(),null;let n=i,a=new AbortController,o={passive:!0,signal:a.signal},u=matchMedia("(prefers-reduced-motion: reduce)"),s={x:0.5,y:0.5,dx:0,dy:0,active:!1};window.addEventListener("pointermove",(l)=>{if(l.pointerType!=="mouse")return;let v=t.getBoundingClientRect(),S=(l.clientX-v.left)/v.width,f=1-(l.clientY-v.top)/v.height,c=S>=0&&S<=1&&f>=0&&f<=1;if(s.active&&c)s.dx+=S-s.x,s.dy+=f-s.y;s.x=S,s.y=f,s.active=c},o),document.addEventListener("pointerleave",()=>{s.active=!1},o),window.addEventListener("blur",()=>{s.active=!1},o);let p=r.adaptive===!1?null:new k({budgetMs:r.budgetMs}),g=p?n.getExtension("EXT_disjoint_timer_query_webgl2"):null,T=[],x,m=null,b=0,d=0,E=!document.hidden,R=!0,h={gl:n,canvas:t,pointer:s,reducedMotion:u,dprCap:r.dprCap??2,get scale(){return p?.scale??1},get dpr(){return Math.min(window.devicePixelRatio||1,h.dprCap)*h.scale},resize(){let l=Math.max(1,Math.round(t.clientWidth*h.dpr)),v=Math.max(1,Math.round(t.clientHeight*h.dpr));if(t.width===l&&t.height===v)return!1;return t.width=l,t.height=v,!0},start(l){m=l,L()},stop(){m=null,cancelAnimationFrame(b),b=0},destroy(){h.stop(),a.abort(),F.disconnect(),_.disconnect(),n.getExtension("WEBGL_lose_context")?.loseContext(),t.remove(),w.get(e)?.delete(h)}};function y(l){if(b=0,!m)return;let v=l-d;if(d=l,g)while(T.length&&n.getQueryParameter(T[0],n.QUERY_RESULT_AVAILABLE)){let c=T.shift();if(!n.getParameter(g.GPU_DISJOINT_EXT))x=n.getQueryParameter(c,n.QUERY_RESULT)/1e6;n.deleteQuery(c)}if(!(u.matches||!!r.held?.())&&p?.sample(v,x)&&h.resize())x=void 0,r.onResize?.();let f=g&&T.length<4?n.createQuery():null;if(f)n.beginQuery(g.TIME_ELAPSED_EXT,f);if(m(Math.min(Math.max(v/1000,0),0.03333333333333333),v),f)n.endQuery(g.TIME_ELAPSED_EXT),T.push(f);s.dx=0,s.dy=0,L()}function L(){if(b||!m||!E||!R)return;if(!d)d=performance.now();b=requestAnimationFrame(y)}function A(){cancelAnimationFrame(b),b=0}document.addEventListener("visibilitychange",()=>{if(E=!document.hidden,!E)return A();d=performance.now(),L()},{signal:a.signal});let _=new IntersectionObserver(([l])=>{if(R=l.isIntersecting,!R)return A();d=performance.now(),L()});_.observe(t);let F=new ResizeObserver(()=>{if(!h.resize())return;if(r.onResize?.(),m&&E&&R)m(0,0),s.dx=0,s.dy=0});if(F.observe(t),!w.has(e))w.set(e,new Set);return w.get(e).add(h),h.resize(),h}function X(e,r,t){let i=e.createShader(r);if(e.shaderSource(i,t),e.compileShader(i),!e.getShaderParameter(i,e.COMPILE_STATUS)){let n=e.getShaderInfoLog(i);throw Error(`Shader compile failed: ${n}
---
${t}`)}return i}class P{gl;program;uniforms={};constructor(e,r,t){this.gl=e;let i=X(e,e.VERTEX_SHADER,r),n=X(e,e.FRAGMENT_SHADER,t),a=e.createProgram();if(e.attachShader(a,i),e.attachShader(a,n),e.linkProgram(a),!e.getProgramParameter(a,e.LINK_STATUS))throw Error(`Program link failed: ${e.getProgramInfoLog(a)}`);e.deleteShader(i),e.deleteShader(n),this.program=a;let o=e.getProgramParameter(a,e.ACTIVE_UNIFORMS);for(let u=0;u<o;u++){let{name:s}=e.getActiveUniform(a,u);this.uniforms[s]=e.getUniformLocation(a,s)}}use(){this.gl.useProgram(this.program)}dispose(){this.gl.deleteProgram(this.program)}}function N(e,r,t,i,n,a,o){let u=e.createTexture();e.activeTexture(e.TEXTURE0),e.bindTexture(e.TEXTURE_2D,u),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_MIN_FILTER,o),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_MAG_FILTER,o),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_WRAP_S,e.CLAMP_TO_EDGE),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_WRAP_T,e.CLAMP_TO_EDGE),e.texImage2D(e.TEXTURE_2D,0,i,r,t,0,n,a,null);let s=e.createFramebuffer();return e.bindFramebuffer(e.FRAMEBUFFER,s),e.framebufferTexture2D(e.FRAMEBUFFER,e.COLOR_ATTACHMENT0,e.TEXTURE_2D,u,0),e.viewport(0,0,r,t),e.clearColor(0,0,0,0),e.clear(e.COLOR_BUFFER_BIT),{texture:u,fbo:s,width:r,height:t,texelSizeX:1/r,texelSizeY:1/t,attach(p){return e.activeTexture(e.TEXTURE0+p),e.bindTexture(e.TEXTURE_2D,u),p},dispose(){e.deleteTexture(u),e.deleteFramebuffer(s)}}}function q(e){let r=e.createVertexArray();e.bindVertexArray(r);let t=e.createBuffer();return e.bindBuffer(e.ARRAY_BUFFER,t),e.bufferData(e.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),e.STATIC_DRAW),e.enableVertexAttribArray(0),e.vertexAttribPointer(0,2,e.FLOAT,!1,0,0),e.bindVertexArray(null),function(n){if(e.bindVertexArray(r),n==null)e.bindFramebuffer(e.FRAMEBUFFER,null),e.viewport(0,0,e.drawingBufferWidth,e.drawingBufferHeight);else e.bindFramebuffer(e.FRAMEBUFFER,n.fbo),e.viewport(0,0,n.width,n.height);e.drawArrays(e.TRIANGLES,0,3)}}var z=`#version 300 es
precision highp float;
// A real attribute at location 0 (not gl_VertexID): drawing with attrib 0
// disabled forces slow emulation on desktop-GL browsers like Firefox/macOS.
layout(location = 0) in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;var C=`
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
`,V=`
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
`,Q=`
vec2 smoke(vec2 n, float t) {
  vec2 w = vec2(fbm(n + vec2(0.0, 0.021 * t)), fbm(n + vec2(5.2, 1.3) - vec2(0.017 * t, 0.0)));
  w = vec2(fbm(n + 2.0 * w + vec2(1.7, 9.2) + 0.012 * t), fbm(n + 2.0 * w + vec2(8.3, 2.8) - 0.01 * t));
  return w - 0.47;
}
`,Y=`
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
  return vec4(toSrgb(col) + (hash12(gl_FragCoord.xy + 71.0) - 0.5) / 255.0, 1.0);
}
`;function K(e,r,t,i){let n=Math.floor(i);e.uniform1f(r.uGrain,t),e.uniform2f(r.uGrainSeeds,n%256,(n+1)%256),e.uniform1f(r.uGrainMix,i-n)}var j=`
void rainbow(float h, out vec3 mid, out vec3 high) {
  high = vec3(0.74, 0.17 * cos(h), 0.17 * sin(h));
  mid = vec3(0.6, 0.16 * cos(h + 1.3), 0.16 * sin(h + 1.3));
}
`;var J=`#version 300 es
precision highp float;
in vec2 vUv;
uniform vec2 uBox;      // box size in units
uniform float uTime;
uniform float uWarp;    // smokiness
uniform vec2 uSeed;     // noise domain offset
uniform vec3 uPhase;    // phase offsets: band a, band b, distortion
out vec4 fragColor;
${C}
${Q}

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
`,Z=`#version 300 es
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
${C}
${V}
${Y}
${j}

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
}
`;var G={ember:["#120602","#9b3b1a","#d8662a"]},le={palette:"ember",intensity:1,speed:1,scale:1,warp:0.35,grain:0.04,grainPeriod:2.5,fadeBottom:0.8,fadeSide:-0.6,seed:0,motion:!0},fe=700,U=Math.PI*2;function me(e){let r=parseInt(e.slice(1),16);return[(r>>16&255)/255,(r>>8&255)/255,(r&255)/255]}function M(e){let r=Math.sin(e*12.9898+78.233)*43758.5453;return r-Math.floor(r)}function ee(e,r={}){let t={...le,...r},i=()=>{},n="",a=W(e,{floatTargets:!0,held:()=>!t.motion,onResize:()=>{i(),n=""}});if(!a)return e.classList.add("haze-fallback"),{config:t,destroy(){}};let{gl:o,canvas:u,reducedMotion:s}=a,p;try{p=[new P(o,z,J),new P(o,z,Z)]}catch{return a.destroy(),e.classList.add("haze-fallback"),{config:t,destroy(){}}}let[g,T]=p,x=q(o),m=null;i=()=>{m?.dispose();let h=Math.max(1,Math.round(u.clientWidth/2)),y=Math.max(1,Math.round(u.clientHeight/2));m=N(o,h,y,o.RGBA16F,o.RGBA,o.HALF_FLOAT,o.LINEAR)},i();let b=0,d=s.matches||!t.motion?1:0,E=0;function R(h,y){if(s.matches||!t.motion){d=1;let D=JSON.stringify(t);if(D===n)return;n=D}else if(n="",b+=h*t.speed,d=Math.min(1,d+y/fe),t.grainPeriod>0)E+=y/1000/t.grainPeriod;let L=G[t.palette]??G.ember,[A,_,F]=L.map(me),l=u.clientWidth||1,v=u.clientHeight||1,S=Math.min(v,Math.max(l,v)/4)*Math.max(t.scale,0.05),f=t.seed;g.use();let c=g.uniforms;if(o.uniform2f(c.uBox,l/S,v/S),o.uniform1f(c.uTime,b),o.uniform1f(c.uWarp,t.warp),o.uniform2f(c.uSeed,M(f+4)*97,M(f+5)*97),f)o.uniform3f(c.uPhase,M(f)*U,M(f+1)*U,M(f+2)*U);else o.uniform3f(c.uPhase,0,0,0);x(m),T.use(),c=T.uniforms,o.uniform1i(c.uGlow,m.attach(0)),o.uniform3fv(c.uBase,A),o.uniform3fv(c.uMid,_),o.uniform3fv(c.uHigh,F),o.uniform1f(c.uIntensity,t.intensity),o.uniform1f(c.uReveal,1-Math.pow(1-d,3)),o.uniform1f(c.uFadeBottom,t.fadeBottom),o.uniform1f(c.uFadeSide,t.fadeSide),K(o,c,t.grain,E),o.uniform1f(c.uRainbow,0),x(null)}return R(0,0),a.start(R),{config:t,destroy(){a.destroy()}}}var ve={warp:0.4,grain:0.05,fadeBottom:0.9,fadeSide:0.55,speed:0.8};function pe(e){let r=getComputedStyle(e),t=(a)=>r.getPropertyValue(a).trim(),i=[t("--haze-ground"),t("--haze-back"),t("--haze-front")],n=`landing:${i.join(":")}`;return G[n]=i,{palette:n,intensity:Number(t("--haze-strength"))}}function re(e,r,t){let i=t("(prefers-color-scheme: dark)"),n=t("(prefers-reduced-motion: reduce)"),a=null,o=!1,u=()=>{},s=()=>{let d=r.getAttribute("data-theme");return d==="dark"||d!=="light"&&i.matches};function p(){u(),u=()=>{},a?.destroy(),a=null}function g(){p(),o=!0}function T(){o=!0;let d;try{d=ee(e,{...ve,...pe(r),motion:!n.matches})}catch{H(e),e.querySelector("canvas")?.remove();return}let E=e.querySelector("canvas");if(!E)return;a=d,o=!1,E.addEventListener("webglcontextlost",g,{once:!0}),u=()=>E.removeEventListener("webglcontextlost",g)}let x=()=>{if(!s())p(),o=!1;else if(!a&&!o)T()},m=()=>{if(a)a.config.motion=!n.matches},b=new MutationObserver(x);return b.observe(r,{attributes:!0,attributeFilter:["data-theme"]}),i.addEventListener("change",x),n.addEventListener("change",m),x(),()=>{b.disconnect(),i.removeEventListener("change",x),n.removeEventListener("change",m),p()}}var te=document.currentScript&&document.querySelector(".haze");if(te)re(te,document.documentElement,(e)=>matchMedia(e));})();
