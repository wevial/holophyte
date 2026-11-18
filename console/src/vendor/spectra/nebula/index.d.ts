// Vendored from wevial/spectra@d34e0f8 cursors/nebula/src/index.d.ts (MIT, see ../LICENSE), given a motion option.
export interface NebulaConfig {
  simResolution: number;
  offsetResolution: number;
  cloudResolution: number;
  pressureIterations: number;
  velocityDissipation: number;
  offsetDecay: number;
  splatForce: number;
  /** Drift speed of the cloud. */
  speed: number;
  /** Ambient wanderers stirring the gas; 0 = none. */
  currents: number;
  /** 'space': stars and glowing gas. 'paint': pigment on a flat ground. */
  look: 'space' | 'paint';
  /** The paint look's palette, a key of PAINTS. Changing it resets ground. */
  paint: string;
  /** The paint look's ground color, hex. */
  ground: string;
  /** false freezes the cloud on one frame, as reduced motion does. */
  motion: boolean;
}
export const CONFIG: NebulaConfig;
/** Paint-look palettes: inks for the gas's fringe, middle, core and hot core, over a ground. */
export const PAINTS: Record<string, { inks: [string, string, string, string]; ground: string; glow?: 'inks' | 'prism' | 'rainbow' }>;
export function mount(
  container: HTMLElement,
  opts?: {
    tier?: number;
    debug?: boolean;
    /** false: a background; the pointer is ignored. Default true. */
    cursor?: boolean;
    /** Ambient currents; default 0 with the cursor, 1 without. */
    currents?: number;
    speed?: number;
    look?: 'space' | 'paint';
    paint?: string;
    ground?: string;
    /** false freezes the cloud on one frame, as reduced motion does. */
    motion?: boolean;
  },
): { config: NebulaConfig; destroy(): void };
