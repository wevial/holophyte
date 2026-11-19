// Vendored from wevial/spectra@4d4c754 cursors/nebula/src/paints.js (MIT, see ../LICENSE).
// Palettes for the paint look. The gas has three hues (blue fringes, purple
// middles, pink cores) plus hot white-pink at its densest; the paint look
// swaps each for an ink, in that order, over a ground. A paint with `glow`
// emits light over a dark ground instead of laying ink down: `inks` glows in
// its inks, `prism` in silver that splits into a spectrum at the gas's edges,
// `rainbow` in a slow rainbow drifting across the box (those two ignore inks).

/** Glow kinds, in the shader's order. */
export const GLOWS = /** @type {const} */ (['inks', 'prism', 'rainbow']);

/** @type {Record<string, { inks: [string, string, string, string], ground: string, glow?: typeof GLOWS[number] }>} */
export const PAINTS = {
  rainbow: { inks: ['#6b6bff', '#3fe0c8', '#ff6fb6', '#ffffff'], ground: '#07060a', glow: 'rainbow' },
  spectrum: { inks: ['#6b6bff', '#3fe0c8', '#ffcf5a', '#ff9ad0'], ground: '#07060a', glow: 'inks' },
  prism: { inks: ['#c4c8d0', '#d0cac2', '#bcc4d0', '#ffffff'], ground: '#07060a', glow: 'prism' },
  nebula: { inks: ['#398ce6', '#7c4ddb', '#e65aa8', '#e6a9cf'], ground: '#efe8dc' },
  marbling: { inks: ['#1f3a6b', '#c98a2b', '#8c2f2a', '#e3cf9f'], ground: '#f1e9d6' },
  riso: { inks: ['#0078bf', '#ff48b0', '#ffd400', '#ff6c2f'], ground: '#f7f4ee' },
  ink: { inks: ['#2b2f38', '#1c2c48', '#0e0e12', '#3d4a60'], ground: '#ece6d8' },
  lagoon: { inks: ['#143d59', '#0f8b8d', '#5fd3c6', '#e8fff8'], ground: '#efe6d2' },
  candy: { inks: ['#5cbcff', '#a184ff', '#ff6fb1', '#ffd86b'], ground: '#fff8f2' },
  sunset: { inks: ['#c0396b', '#ff6b35', '#f7c548', '#ffe3c2'], ground: '#2a1838' },
  moss: { inks: ['#2f5d3a', '#7fa36b', '#c9b458', '#efe7c4'], ground: '#161a14' },
};
