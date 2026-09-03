# SwiftBar drawer v0

`holophyte.10s.py` is a [SwiftBar](https://swiftbar.app) plugin that polls one
`--serve` daemon per target (see `docs/operating.md`, "Serving") and puts
"what should I look at next?" one glance away in the menu bar. It needs only
Python 3.11+ and the standard library.

## Install

1. Install SwiftBar and pick a plugin folder in its preferences.
2. Symlink the script into that folder (a symlink, not a copy: the script
   finds the two-leaf icon in `assets/` relative to its own repository):

   ```
   ln -s /path/to/holophyte/contrib/swiftbar/holophyte.10s.py ~/SwiftBar/
   ```

   The `10s` in the name is SwiftBar's refresh interval.
3. Write the config file at `~/.holophyte/drawer.toml` (or under
   `$HOLOPHYTE_HOME` when that is set), one `[[daemon]]` per target. The
   writer host serves holophyte on 7710 and lotuspod on 7711 on its tailnet
   address:

   ```toml
   linear = "https://linear.app/your-workspace"

   [[daemon]]
   name = "holophyte"
   url = "http://writer.tailnet:7710"

   [[daemon]]
   name = "lotuspod"
   url = "http://writer.tailnet:7711"
   ```

4. Refresh SwiftBar. If the icon does not appear, SwiftBar's `PATH` may lack
   a `python3` of 3.11 or newer; put one first on the PATH SwiftBar sees.

To see the exact menu without a network, render fixtures instead:

```
python3 contrib/swiftbar/holophyte.10s.py --render tests/fixtures/drawer/idle.json
```

## The dot

The menu-bar glyph is the two-leaf template icon, embedded as the 18 pt PDF
in `assets/` (the 1x PNG if the PDF is missing): SwiftBar sizes a base64
raster by its pixels, so the 36 px Retina PNG would draw at twice menu-bar
height, while a PDF is 18 points and crisp at any scale. A dot beside it
mirrors the worst row of the menu:

| dot | meaning |
|---|---|
| none | every target is idle with a live supervisor |
| green | at least one run is live and nothing needs you |
| amber | something needs you: a run's heartbeat is older than the daemon's `heartbeat_stale_ms`, or a supervisor is `stale` or `none` |
| red | a daemon did not answer within two seconds (`unreachable`) |

## What "needs you" means

The `NEEDS YOU` section appears only when it has rows, above the per-target
blocks. A row is something the factory cannot fix on its own right now: a run
whose heartbeat has gone stale (the supervisor will sweep it after its strike
count, but a stuck review is worth a look first), a supervisor that is stale
or absent (nothing will sweep anything until it is back), or a daemon that
cannot be reached (you know nothing about that target until it is).

Every age in the menu comes from the daemon's own `now` and `_ms` fields,
never from the Mac's clock, so a reading is never distorted by clock skew.
The footer's "updated Ns ago" is how far the stalest answer trails the
freshest one. Detail rows are dimmed lines indented under their target
(leading spaces kept with `trim=false`) rather than a `--` submenu, so the
detail is in view instead of behind a hover. The footer counts targets (one
per daemon) and distinct hosts separately, since several daemons can live on
one host.

Out of scope for v0: any write action, live streaming (SSE), and
"failed since you last looked" tracking, which needs client state.
