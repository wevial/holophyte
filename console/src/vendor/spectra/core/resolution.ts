// Vendored from wevial/spectra@9f2195d packages/core/src/resolution.ts (MIT, see ../LICENSE).
// Adaptive resolution: picks a render scale (a multiple of the device pixel
// ratio) that keeps each frame's GPU work within budget, so effects stay
// smooth on weak GPUs and run at full resolution on strong ones.
//
// With GPU timer queries (Chrome/ANGLE) it knows the actual GPU cost and,
// since cost scales with pixel count (scale²), jumps straight to a level that
// fits. Without them (Safari, Firefox) it watches rAF intervals relative to
// the display's own refresh, steps one level at a time, and won't climb back
// into a level it just fell out of for a while (that would oscillate).

export const LEVELS = [1, 0.85, 0.7, 0.6, 0.5];

const SETTLE_MS = 1000;        // ignore samples right after a change (realloc jank)
const STALL_MS = 250;          // longer gaps are tab switches or sleep, not load
const DEMOTE_AFTER_MS = 1000;
const PROMOTE_AFTER_MS = 3000;
const FRAME_DEMOTE_AFTER_MS = 1500;
const FRAME_PROMOTE_AFTER_MS = 6000;
const BLOCK_MS = 30000;

export interface AdaptiveResolutionOptions {
  /** GPU ms per frame to stay under. */
  budgetMs?: number;
}

export class AdaptiveResolution {
  level = 0;
  private budget: number;
  private settle = 0;
  private gpu = -1;     // EMA of GPU ms; -1 until sampled
  private frame = -1;   // EMA of the rAF interval
  private refresh = Infinity; // shortest interval seen ≈ the display's refresh
  private over = 0;
  private under = 0;
  private blocked = new Map<number, number>(); // level -> ms until it may be re-entered

  constructor({ budgetMs = 6 }: AdaptiveResolutionOptions = {}) {
    this.budget = budgetMs;
  }

  get scale() {
    return LEVELS[this.level];
  }

  /**
   * Feed one frame: its rAF interval and, when available, the GPU time of a
   * recent frame. Returns true when the scale changed.
   */
  sample(frameMs: number, gpuMs?: number): boolean {
    if (!(frameMs > 0) || frameMs > STALL_MS) return false;
    for (const [l, t] of this.blocked) {
      if (t <= frameMs) this.blocked.delete(l);
      else this.blocked.set(l, t - frameMs);
    }
    this.refresh = Math.min(this.refresh, Math.max(frameMs, 6));
    if (this.settle > 0) {
      this.settle -= frameMs;
      return false;
    }
    if (gpuMs != null) {
      this.gpu = this.gpu < 0 ? gpuMs : this.gpu + (gpuMs - this.gpu) * 0.1;
      return this.byGpu(frameMs);
    }
    this.frame = this.frame < 0 ? frameMs : this.frame + (frameMs - this.frame) * 0.1;
    return this.byFrame(frameMs);
  }

  private byGpu(dt: number) {
    // Predicted GPU ms at level l, from what this level costs now.
    const cost = (l: number) => this.gpu * (LEVELS[l] / this.scale) ** 2;
    if (this.gpu > this.budget) {
      this.over += dt;
      this.under = 0;
      if (this.over > DEMOTE_AFTER_MS) {
        let l = this.level;
        while (l < LEVELS.length - 1 && cost(l) > this.budget) l++;
        return this.set(l);
      }
    } else if (this.level > 0 && cost(this.level - 1) < this.budget * 0.8) {
      this.under += dt;
      this.over = 0;
      if (this.under > PROMOTE_AFTER_MS) return this.set(this.level - 1);
    } else {
      this.over = this.under = 0;
    }
    return false;
  }

  private byFrame(dt: number) {
    // Slow means well behind this display's refresh (a steady 30Hz screen
    // is fine), or under ~22fps on any display.
    const slow = this.frame > Math.max(this.refresh * 1.5, 20) || this.frame > 45;
    if (slow) {
      this.under = 0;
      this.over += dt;
      if (this.over > FRAME_DEMOTE_AFTER_MS && this.level < LEVELS.length - 1) {
        this.blocked.set(this.level, BLOCK_MS);
        return this.set(this.level + 1);
      }
    } else if (this.frame < this.refresh * 1.15 && this.level > 0 && !this.blocked.has(this.level - 1)) {
      this.over = 0;
      this.under += dt;
      if (this.under > FRAME_PROMOTE_AFTER_MS) return this.set(this.level - 1);
    } else {
      this.over = this.under = 0;
    }
    return false;
  }

  private set(level: number) {
    this.over = this.under = 0;
    if (level === this.level) return false;
    this.level = level;
    this.settle = SETTLE_MS;
    this.gpu = this.frame = -1;
    return true;
  }
}
