// Vendored from wevial/spectra@d34e0f8 cursors/nebula/src/perf.js (MIT, see ../LICENSE).
// FPS governor: walks down a quality ladder when frame times sag, back up
// when there's headroom. Tiers trade cloud detail first (the dominant cost),
// then sim resolution, then output resolution. Past the last tier the page
// swaps to the static CSS gradient.

export const TIERS = [
  { name: 'full',   dpr: 2,    cloud: 1024, sim: 224, iters: 24 },
  { name: 'high',   dpr: 1.5,  cloud: 768,  sim: 192, iters: 20 },
  { name: 'medium', dpr: 1.25, cloud: 512,  sim: 144, iters: 16 },
  { name: 'low',    dpr: 1,    cloud: 384,  sim: 112, iters: 12 },
];

// Demotion threshold sits above a 30Hz display's natural 33ms cadence: rAF
// intervals track the refresh rate even when rendering is instant, so a
// tighter threshold would read a healthy 30Hz screen as overload and walk
// all the way to the static fallback. Only sustained sub-25fps demotes.
const DEMOTE_MS = 40;      // sustained frame time above this -> step down
const PROMOTE_MS = 15;     // sustained frame time below this -> step up
const DEMOTE_AFTER_S = 2;  // how long "sustained" is for demotion
const PROMOTE_AFTER_S = 8; // promotion is deliberately slow
const SETTLE_S = 1.5;      // ignore frames right after a tier change (rebuild jank)

export class Governor {
  // onApply(tier) reconfigures the renderer; onFallback() swaps to static CSS.
  // tier locks a quality tier (TIERS.length = static fallback).
  constructor({ onApply, onFallback, tier, debug = false }) {
    this.onApply = onApply;
    this.onFallback = onFallback;
    this.debug = debug;
    this.locked = tier != null;

    const coarse = matchMedia('(pointer: coarse)').matches;
    this.tier = this.locked
      ? Math.min(Math.max(Math.trunc(tier) || 0, 0), TIERS.length)
      : (coarse ? 2 : 0);

    this.ema = 16.7;
    this.badTime = 0;
    this.goodTime = 0;
    this.settle = SETTLE_S;
    this.demotedFrom = new Set(); // tiers we fell out of; never promote back into them

    // The initial tier is NOT applied via onApply: the caller reads `tier`
    // and constructs the renderer at that quality directly, so full-size
    // buffers are never allocated just to be thrown away on weak devices.
    if (this.tier >= TIERS.length) {
      this.onFallback();
    } else if (this.debug) {
      console.info(`[perf] initial tier: ${TIERS[this.tier].name}`);
    }
  }

  apply() {
    if (this.debug) console.info(`[perf] tier -> ${TIERS[this.tier].name}`);
    this.onApply(TIERS[this.tier]);
    this.settle = SETTLE_S;
    this.badTime = 0;
    this.goodTime = 0;
  }

  // Call once per frame with the raw frame delta in ms. Returns false once
  // the governor has bailed out to the static fallback.
  frame(dtMs) {
    if (this.tier >= TIERS.length) return false;
    if (this.locked) return true;

    // A single huge gap is a suspension (system sleep, background stall),
    // not sustained overload — one sample would otherwise blow through both
    // the EMA and the demotion window at once.
    if (dtMs > 500) return true;

    const dtS = dtMs / 1000;
    if (this.settle > 0) {
      this.settle -= dtS;
      return true;
    }

    this.ema += (dtMs - this.ema) * 0.05;

    if (this.ema > DEMOTE_MS) {
      this.badTime += dtS;
      this.goodTime = 0;
      if (this.badTime > DEMOTE_AFTER_S) {
        this.demotedFrom.add(this.tier);
        this.tier++;
        this.ema = 16.7;
        if (this.tier >= TIERS.length) {
          if (this.debug) console.info('[perf] tier -> static fallback');
          this.onFallback();
          return false;
        }
        this.apply();
      }
    } else if (this.ema < PROMOTE_MS && this.tier > 0 && !this.demotedFrom.has(this.tier - 1)) {
      this.goodTime += dtS;
      this.badTime = 0;
      if (this.goodTime > PROMOTE_AFTER_S) {
        this.tier--;
        this.ema = 16.7;
        this.apply();
      }
    } else {
      this.badTime = 0;
      this.goodTime = 0;
    }
    return true;
  }
}
