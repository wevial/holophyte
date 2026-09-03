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

A fixture is a `/status` answer, or an object with `status` and `runs` keys
carrying the `/runs` answer too (`idle_last_merge.json`), which is what an
idle target's "last merge" row reads.

## The dot

The menu-bar glyph is the two-leaf icon from `assets/`, 24 × 20 points as a
vector PDF so it is crisp at any scale (SwiftBar sizes a base64 raster by
its pixels, which is why the 36 px Retina PNG is never embedded). When every
target is idle the title carries `menubar-template.pdf` as `templateImage=`,
which macOS tints for the bar. Otherwise the state dot is drawn inside the
image, at the glyph's top-right, so the title carries one of the three
pre-rendered variants as a plain `image=`:

| file | when |
|---|---|
| `menubar-template.pdf` | idle (`templateImage=`) |
| `menubar-{ok,warn,bad}.pdf` | white leaves with a thin dark outline |

There is one design per level, not a dark and a light one: macOS tints the
menu bar from the wallpaper behind it, not from the system appearance, and
nothing SwiftBar passes to a plugin says what that tint is (`OS_APPEARANCE`
reports the system setting, so a light system over a dark wallpaper band
drew black leaves on a black bar). The outlined white glyph reads on both.
If a variant file is missing (a partial checkout), the script falls back to
the template glyph with a coloured `●` printed beside it, so state still
shows. `ok`/`warn`/`bad` is the worst row of the menu:

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

An idle target's row names what it last shipped: `idle · last merge KO-n ·
12m ago`, read from a second request, `GET /runs`, that the plugin makes only
for an idle daemon. It reads the whole table and takes its newest `merged`
row, deliberately not `?limit=1`: `/runs` lists oldest first and `?limit=N`
keeps the first N, so `?limit=1` would name the first run ever, not the last
merge. The daemon's rows do not carry an end time today, so a
live row reads `idle · last merge KO-n` without the age until they do; when
`/runs` does not answer, the row falls back to `idle · queue empty`. The
supervisor row is the state word alone when it is `live`; `stale · 7m` and
`none` show the age and turn amber, since the age only says something when
the state is wrong. These two rows print ages in whole units the way the
daemon's tables do (`12s`, `7m`, `2h`, `3d`); a working row keeps its
minute-precise `1h02m of 2h00m`, where the minutes are the point.

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
