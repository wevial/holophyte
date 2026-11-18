# Spectra

Vendored from [wevial/spectra](https://github.com/wevial/spectra) (MIT, see
`LICENSE`) at commit `d34e0f8`. Haze was taken at `9f2195d`; its source is the
same at `d34e0f8`.

| Folder | Spectra path |
| --- | --- |
| `core` | `packages/core/src` (`gl`, `glsl`, `resolution`, `stage`) |
| `haze` | `backgrounds/haze/src` |
| `caustics` | `backgrounds/caustics/src` |
| `plume` | `backgrounds/plume/src` |
| `ribbons` | `backgrounds/ribbons/src` |
| `lava` | `backgrounds/lava/src` |
| `nebula` | `cursors/nebula/src` |
| `film` | `cursors/film/src/spectrum.ts`, the part Caustics imports |

Each file is Spectra's own apart from these changes, so a later sync stays a
copy:

- Imports of `@spectra/core`, `@spectra/film` and `@spectra/nebula` point at
  the sibling folders.
- Haze, Caustics, Ribbons, Lava and Nebula (so Plume) take a `motion` option:
  `false` draws one still frame, as the system's reduced-motion setting does.
- The stage takes a `held` option, which those effects set from `motion`:
  while a still frame is held, or reduced motion is on, slow frames do not
  shrink the drawing buffer and so do not redraw the frame.
- Nebula's still frame draws once and holds until its cloud, config or
  drawing buffer changes, its quality governor rests while the frame is held,
  and its textures are capped to the GPU's limit, which a wide header band
  would exceed.
- Haze is trimmed to the `ember` palette the console overrides, and falls back
  when its shaders fail to compile.
