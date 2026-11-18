// Vendored from wevial/spectra@d34e0f8 backgrounds/plume/src/index.ts (MIT, see ../LICENSE), given a motion option.
// Plume background — the nebula's gas dragged like wet paint. It's the
// nebula cursor without the cursor: a few invisible wanderers pull lanes
// through its fluid sim, so the gas swirls, thins and piles up at the edges,
// then heals back, on its own. Ignores the pointer.

import { mount as mountNebula, PAINTS } from '../nebula';
import type { NebulaConfig } from '../nebula';

export type { NebulaConfig };

/** Palettes: four inks (the gas's fringe, middle, core, hot core) over a ground. */
export { PAINTS };

export interface PlumeOptions {
  /** Palette, a key of PAINTS. Default `rainbow`. */
  paint?: keyof typeof PAINTS;
  /** Strength of the ambient currents; 0 leaves only the drift. */
  currents?: number;
  /** Drift speed of the cloud and the currents. */
  speed?: number;
  /** Ground color under the paint, hex. Default the palette's. */
  ground?: string;
  /** Lock a quality tier, 0–3 (4 = static fallback). */
  tier?: number;
  /** Log the quality governor's decisions. */
  debug?: boolean;
  /** false freezes the cloud on one frame, as reduced motion does. */
  motion?: boolean;
}

/**
 * Mount the background into `container` (which should be positioned and
 * sized). `config` is live; `paint`, `currents` and `speed` are the ones to
 * tune. Without WebGL2 + float targets the container gets the
 * `nebula-fallback` class instead.
 */
export function mount(container: HTMLElement, opts: PlumeOptions = {}) {
  return mountNebula(container, { ...opts, paint: opts.paint ?? 'rainbow', cursor: false, look: 'paint' });
}
