// Vendored from wevial/spectra@d34e0f8 packages/core/src/stage.ts (MIT, see ../LICENSE).
// Host plumbing every effect needs: a canvas filling its container, a WebGL2
// context, DPR-capped sizing, mouse tracking in GL uv space, and a render loop
// that pauses while the tab is hidden or the canvas is scrolled out of view.
// Resolution adapts to the GPU: each frame is timed (GPU timer queries where
// available) and the drawing buffer shrinks when frames run over budget.

import { AdaptiveResolution } from './resolution';

export interface Pointer {
  x: number;        // GL uv, origin bottom-left
  y: number;
  dx: number;       // movement accumulated since the last frame
  dy: number;
  active: boolean;  // false until the first move, and after the mouse leaves
}

export interface Stage {
  gl: WebGL2RenderingContext;
  canvas: HTMLCanvasElement;
  pointer: Pointer;
  reducedMotion: MediaQueryList;
  dprCap: number;
  /** Device px per CSS px actually rendered: DPR, capped, times the adaptive scale. */
  readonly dpr: number;
  /** Current adaptive resolution scale, 0.5..1. */
  readonly scale: number;
  /** Resize the drawing buffer to the canvas box. Returns true if it changed. */
  resize(): boolean;
  start(frame: (dt: number, rawDtMs: number) => void): void;
  stop(): void;
  destroy(): void;
}

export interface StageOptions {
  dprCap?: number;
  /** Ask for EXT_color_buffer_float (render to half/float targets). */
  floatTargets?: boolean;
  /** Shrink the drawing buffer when the GPU falls behind (default true). */
  adaptive?: boolean;
  /** GPU ms per frame the adaptive resolution aims to stay under (default 6). */
  budgetMs?: number;
  onResize?: () => void;
}

/** Returns null when WebGL2 (or float targets, if asked) is unavailable. */
export function createStage(container: HTMLElement, opts: StageOptions = {}): Stage | null {
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;';
  container.appendChild(canvas);

  const context = canvas.getContext('webgl2', {
    alpha: false,
    depth: false,
    stencil: false,
    antialias: false,
    powerPreference: 'high-performance',
  });
  if (!context || (opts.floatTargets && !context.getExtension('EXT_color_buffer_float'))) {
    canvas.remove();
    return null;
  }
  const gl: WebGL2RenderingContext = context;

  const ac = new AbortController();
  const listen = { passive: true, signal: ac.signal } as const;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

  // Mouse only — touch is left free for scrolling/gestures. Coordinates are
  // relative to the canvas, so an effect can live in any box on the page.
  const pointer: Pointer = { x: 0.5, y: 0.5, dx: 0, dy: 0, active: false };
  window.addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return;
    const r = canvas.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = 1 - (e.clientY - r.top) / r.height;
    const inside = x >= 0 && x <= 1 && y >= 0 && y <= 1;
    if (pointer.active && inside) {
      pointer.dx += x - pointer.x;
      pointer.dy += y - pointer.y;
    }
    pointer.x = x;
    pointer.y = y;
    pointer.active = inside;
  }, listen);
  document.addEventListener('pointerleave', () => { pointer.active = false; }, listen);
  window.addEventListener('blur', () => { pointer.active = false; }, listen);

  const adaptive = opts.adaptive === false ? null : new AdaptiveResolution({ budgetMs: opts.budgetMs });
  // GPU time per frame, read back a few frames late. Disjoint results
  // (power state changes, context switches) are dropped.
  const timer = adaptive ? gl.getExtension('EXT_disjoint_timer_query_webgl2') : null;
  const queries: WebGLQuery[] = [];
  let gpuMs: number | undefined;

  let frameFn: ((dt: number, rawDtMs: number) => void) | null = null;
  let rafId = 0;
  let last = 0;
  let visible = !document.hidden;
  let onScreen = true;

  const stage: Stage = {
    gl,
    canvas,
    pointer,
    reducedMotion,
    dprCap: opts.dprCap ?? 2,
    get scale() {
      return adaptive?.scale ?? 1;
    },
    get dpr() {
      return Math.min(window.devicePixelRatio || 1, stage.dprCap) * stage.scale;
    },
    resize() {
      const w = Math.max(1, Math.round(canvas.clientWidth * stage.dpr));
      const h = Math.max(1, Math.round(canvas.clientHeight * stage.dpr));
      if (canvas.width === w && canvas.height === h) return false;
      canvas.width = w;
      canvas.height = h;
      return true;
    },
    start(frame) {
      frameFn = frame;
      schedule();
    },
    stop() {
      frameFn = null;
      cancelAnimationFrame(rafId);
      rafId = 0;
    },
    destroy() {
      stage.stop();
      ac.abort();
      ro.disconnect();
      io.disconnect();
      gl.getExtension('WEBGL_lose_context')?.loseContext();
      canvas.remove();
    },
  };

  function tick(now: number) {
    rafId = 0;
    if (!frameFn) return;
    const rawDt = now - last;
    last = now;

    if (timer) {
      while (queries.length && gl.getQueryParameter(queries[0], gl.QUERY_RESULT_AVAILABLE)) {
        const q = queries.shift()!;
        if (!gl.getParameter(timer.GPU_DISJOINT_EXT)) gpuMs = gl.getQueryParameter(q, gl.QUERY_RESULT) / 1e6;
        gl.deleteQuery(q);
      }
    }
    if (adaptive?.sample(rawDt, gpuMs) && stage.resize()) {
      gpuMs = undefined;
      opts.onResize?.();
    }

    const q = timer && queries.length < 4 ? gl.createQuery() : null;
    if (q) gl.beginQuery(timer!.TIME_ELAPSED_EXT, q);
    frameFn(Math.min(Math.max(rawDt / 1000, 0), 1 / 30), rawDt);
    if (q) {
      gl.endQuery(timer!.TIME_ELAPSED_EXT);
      queries.push(q);
    }
    pointer.dx = 0;
    pointer.dy = 0;
    schedule();
  }

  function schedule() {
    if (rafId || !frameFn || !visible || !onScreen) return;
    if (!last) last = performance.now();
    rafId = requestAnimationFrame(tick);
  }

  function pause() {
    cancelAnimationFrame(rafId);
    rafId = 0;
  }

  document.addEventListener('visibilitychange', () => {
    visible = !document.hidden;
    if (!visible) return pause();
    last = performance.now();
    schedule();
  }, { signal: ac.signal });

  const io = new IntersectionObserver(([entry]) => {
    onScreen = entry.isIntersecting;
    if (!onScreen) return pause();
    last = performance.now();
    schedule();
  });
  io.observe(canvas);

  // Resizing the canvas clears its drawing buffer, and ResizeObserver runs
  // after this frame's rAF draw but before paint, so without a redraw here
  // every resize step paints one blank frame: a black flash while dragging.
  // A zero-length frame redraws in place; pointer deltas it consumes are
  // cleared so the next tick doesn't apply them twice.
  const ro = new ResizeObserver(() => {
    if (!stage.resize()) return;
    opts.onResize?.();
    if (frameFn && visible && onScreen) {
      frameFn(0, 0);
      pointer.dx = 0;
      pointer.dy = 0;
    }
  });
  ro.observe(canvas);

  stage.resize();
  return stage;
}
