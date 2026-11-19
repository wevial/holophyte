# Spectra

Vendored from [wevial/spectra](https://github.com/wevial/spectra) (MIT, see
`LICENSE`) at commit `4d4c754`.

| Folder | Spectra path |
| --- | --- |
| `core` | `packages/core/src` (`gl`, `glsl`, `random`, `resolution`, `stage`) |
| `haze` | `backgrounds/haze/src` |
| `caustics` | `backgrounds/caustics/src` |
| `plume` | `backgrounds/plume/src` |
| `ribbons` | `backgrounds/ribbons/src` |
| `lava` | `backgrounds/lava/src` |
| `hyphae` | `backgrounds/hyphae/src` |
| `nebula` | `cursors/nebula/src` |
| `film` | `cursors/film/src/spectrum.ts`, the part Caustics imports |
| `mycelium` | `cursors/mycelium/src/shaders.ts`, the part Hyphae imports |

The Header tiles' previews in `console/public/effects/` (`haze`, `caustics`,
`plume`, `ribbons`, `lava` and `hyphae` `.webp`) are byte-for-byte copies of
Spectra's `apps/gallery/public/previews/` at commit `75ae266`.

Each file is Spectra's own apart from these changes, so a later sync stays a
copy:

- Imports of `@spectra/core`, `@spectra/film`, `@spectra/mycelium` and
  `@spectra/nebula` point at the sibling folders.
- Haze, Caustics, Ribbons, Lava, Hyphae and Nebula (so Plume) take a `motion`
  option: `false` draws one still frame, as the system's reduced-motion setting
  does. Hyphae grows its colony unseen before that frame, as it does under
  reduced motion.
- The stage takes a `held` option, which those effects set from `motion`:
  while a still frame is held, or reduced motion is on, slow frames do not
  shrink the drawing buffer and so do not redraw the frame.
- Nebula's still frame draws once and holds until its cloud, config or
  drawing buffer changes, its quality governor rests while the frame is held,
  and its textures are capped to the GPU's limit, which a wide header band
  would exceed. Its `advance()` runs the currents unseen for a held frame.
- Hyphae's held regrowth after `reset()` leaves the last drawn frame on the
  canvas until the new colony is grown.
- The stage records itself against its container, and `destroyStages`
  destroys what a mount that threw after creating its stage left live.
- Haze's `PALETTES` is typed open, so the console can register its theme
  palette there, and Haze falls back when its shaders fail to compile. Haze
  adds its `haze-fallback` class itself rather than through the stage's
  `fallback` option.
- Transparent Haze keeps its palette's base in the blend and takes its alpha
  from the smoke's coverage, with the base taken out of the color: its lit
  smoke keeps the opaque frame's colors on a page of that base, and unlit
  pixels, grain and vignette included, let any page through.
