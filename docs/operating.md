# Operating

Supervising a target and serving its state read-only. The operator
commands (`--requeue KO-n --note TEXT`, `--file-ticket TICKET.md`) are
described by `factory.py --help`, and the escalation ladder they sit on in
[AGENTS.md](../AGENTS.md). Back to the [README](../README.md).

## Supervising

The loop watches itself only while it is alive. A crashed or hung run leaves
a row in a work phase and a lease nobody gives back, and the supervisor is
what notices: an acting sweep (`--sweep --act`) that fails any run with a
dead heartbeat, a blown time box or a stuck review, releases its leases and
leaves its branch and worktree for a human. `--supervise` runs that sweep
every 60 seconds by default (`[supervisor] sweep_interval_sec`) as a
long-lived process:

```
python3 factory.py --supervise /path/to/repo
```

It runs until SIGINT or SIGTERM, finishing the pass in hand and exiting
clean. One supervisor per target: the first takes
`supervisor.lock` in the target's state directory (beside the store) with an
exclusive create and writes its pid into it; a second `--supervise` for the
same target exits non-zero naming that pid. A lock whose pid is dead is a
supervisor that was killed without the chance to clean up, and is reclaimed
on the next start; reclaims take turns under an flock on the sidecar
`supervisor.lock.reclaim` beside it, which is left in place. A lock
that names no pid at all is not guessed about: the start refuses and says
which file to look at.

Each pass bumps the process's row in the store's `supervisorHeartbeats`
table, so whether the watcher is still watching is a query rather than a
`ps`. The sweep also watches the loop's own restarts: a loop that merges a
change to the factory itself writes a `loopRestarts` row and re-executes,
and if no claim, heartbeat or "no ready tickets" exit follows within
`restart_grace_sec` the next sweep prints `loop did not return after re-exec
from <sha>` and records it, once per restart. The supervisor also watches
the factory checkout it runs from: before each pass it compares that
checkout's `HEAD` with the one it started on, and when they differ -- or
when a pass finds the store stamped with a newer schema than its build
understands -- it prints `factory code moved from OLD to NEW; supervisor
re-executing`, releases its lock and replaces itself with the same command
line, so a self-merge does not end the watch. Nothing else is relaunched. Process management (systemd, a tmux pane, `nohup`) is the operator's;
the factory ships the invocation and nothing around it.

## Serving

`--serve HOST:PORT` runs a read-only HTTP daemon for one target, so a drawer
or dashboard on another host of the tailnet can poll the factory without
ssh:

```
python3 factory.py --serve 100.64.0.1:8787 /path/to/repo
```

It answers two paths as JSON, every response `Cache-Control: no-store`, and
opens the store through a read-only connection per request; it never holds
a connection between requests and never writes. Any other path is 404 and
any method but GET is 405, both as JSON.

| Path | Body |
| --- | --- |
| `GET /status` | `target`, `host`, `now`, `supervisor` (`state` live/stale/none, `pid`, `heartbeat_age_ms`, `host`), `thresholds` (`heartbeat_stale_ms`, `strikes`), and `runs`: one `{id, ticket, phase, heartbeat_age_ms, elapsed_ms, time_box_ms, host}` per live run. 503 when the target has no store yet. |
| `GET /runs?limit=N` | The `--report` table: `rows` of `{ticket, actual_min, estimate_min, ratio, rounds, outcome, host, ended_ms}`, the same rows in the same order as `--report` prints, oldest first, each with `ended_ms` (the run's end as epoch milliseconds, which the table does not print; a drawer ages the last merge from it against `/status`'s `now`); `?limit=N` keeps the first N and echoes `limit` (null when absent). A `limit` that is not a positive integer is 400 with an `error`. |

Every `host` passes through `[report] host_label`, so a configured label is
what the network sees rather than the machine name.

The boundary is the bind address and nothing else. The daemon has
**no authentication**: it binds to the one address the command line names
and anyone who can reach that port can read run and ticket identifiers,
phases, heartbeat ages and the estimate-vs-actual history. Bind it to the
tailnet address only. Binding to `0.0.0.0` publishes that history to every network
the host is on, and there is no flag, token or allow-list in the factory
that would narrow it back; the tailnet's membership is the whole access
control, by design.


## Serving standing

A daemon started by hand in a tmux session ends silently at the next reboot,
and the drawer then reads the host as "attention needed" on every seat.
`deploy/holophyte-serve@.service` is a systemd user unit template that keeps
one daemon per target standing: the instance name is the target slug, the
unit restarts on failure, and an enabled unit comes back after a reboot or a
supervisor re-exec, provided the operator's user manager itself starts at boot
(lingering, below). It runs `factory.py` from the factory checkout named in
its `WorkingDirectory`, so a self-merge is picked up on the next restart; the
daemon reads the store per request and has no state to lose.

The unit reads three keys from `~/.holophyte/SLUG/serve.env`:

| Key | Value |
| --- | --- |
| `HOLOPHYTE_TARGET` | the target repository path |
| `HOLOPHYTE_SERVE_ADDRESS` | the host's tailnet address |
| `HOLOPHYTE_SERVE_PORT` | the port from the convention below |

The address must be the host's tailnet address, never `0.0.0.0` (see
"Serving" above for what an open bind publishes).

**Port convention:** 7710 for the first target on a host, counting up by one
per further target, so a client config is two lines per host. The writer host
today serves `holophyte` on 7710 and `lotuspod` on 7711.

An example `~/.holophyte/holophyte/serve.env`:

```
HOLOPHYTE_TARGET=/path/to/holophyte
HOLOPHYTE_SERVE_ADDRESS=100.64.0.1
HOLOPHYTE_SERVE_PORT=7710
```

Install and enable, one instance per target:

```
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user && cp deploy/holophyte-serve@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now holophyte-serve@holophyte
journalctl --user -u holophyte-serve@holophyte -f
```

The first line matters for an unattended reboot: a user unit is run by the
operator's user manager, and without lingering that manager only starts when
the operator logs in, so an enabled unit would wait for a login that never
comes on a headless writer host. `loginctl enable-linger` starts the user
manager at boot; run it once per host, and check with
`loginctl show-user "$USER" -p Linger` (expect `Linger=yes`).

The unit's `WorkingDirectory` is `%h`-relative and names the checkout
layout of the writer host; adjust it before enabling if the factory lives
elsewhere. A client finds a daemon at the host's tailnet address and the
target's port from the convention, nothing else.
