// Vendored from wevial/spectra@4d4c754 packages/core/src/random.ts (MIT, see ../LICENSE).
// The randomness effects draw on: Math.random by default, a seeded generator
// when a capture needs every run to come out the same.

let source: () => number = Math.random;

/** A number in [0, 1). Use this instead of Math.random in effects. */
export function random() {
  return source();
}

/** Seed `random()` (mulberry32), or pass null to go back to Math.random. */
export function seedRandom(seed: number | null) {
  source = seed == null ? Math.random : mulberry32(seed);
}

export function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
