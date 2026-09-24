# Deploy — units the factory ships but does not start

Process management is the operator's; the factory ships the invocation and
nothing around it. What lives here is the checked-in shape of that management
on a writer host, to copy under `~/.config/systemd/user/` and enable by hand.

## Install

The daemon has one Python dependency, `tomlkit`, which `PUT /config` uses
to edit a project's config in place without losing comments and `project
add` uses to rewrite `host.toml`; it is pinned in `requirements.txt`. On the
writer host, before enabling the host units:

```
python3 -m pip install --user -r requirements.txt
```

A daemon started without it exits with one line naming the module and this
command.

## The host units

One daemon and one sweep per host, for every project in the host registry,
`~/.holophyte/host.toml` (`factory.py project add PATH` writes it). None of
them reads an environment file: they read `host.toml`.

- `deploy/holophyte.target` — `Wants=` the socket and the timer, and is the
  only host unit with an `[Install]` section. `systemctl --user enable --now
  holophyte.target` is the whole setup; `systemctl --user restart
  holophyte.target` restarts the socket, the daemon and the timer, and
  `stop` stops all three.
- `deploy/holophyte-serve.socket` — `ListenStream=127.0.0.1:7710`, `PartOf=`
  the target. systemd holds the port and hands it to the daemon; while no
  daemon runs, connections queue on it. The address is typed here and in
  `host.toml`'s `[serve] bind`: change both together (the daemon names the
  difference once and serves the socket's). A bind beyond loopback needs
  `host.toml`'s `[serve] machine_token_file`.
- `deploy/holophyte-serve.service` — `factory.py --serve`, the host daemon,
  started by the socket on the first connection. `Restart=on-failure`,
  `RestartSec=5` and no start limit (`StartLimitIntervalSec=0`), so a daemon
  that cannot start retries every five seconds with the port held rather
  than failing the socket. On a factory `HEAD` move it drains for at most
  20 s and exits 0 (`TimeoutStopSec=30` covers the drain), and the next
  connection starts the new code: nothing to restart after a merge.
- `deploy/holophyte-sweep.timer` — fires the sweep 5 s after it starts and
  60 s after each run started (`OnUnitActiveSec=60`, `AccuracySec=1`),
  `PartOf=` the target. `OnUnitActiveSec` must equal `host.toml`'s
  `[supervisor] sweep_sec`, 60 by default.
- `deploy/holophyte-sweep.service` — `factory.py --supervise --once`,
  `Type=oneshot`: one run over every registered store, then an exit, 1 when
  any project errored (the unit shows failed; the timer fires again
  regardless). systemd never starts a oneshot that is still activating, so
  runs never overlap. `TimeoutStartSec=120` is above the 97 s worst single
  Linear read; `TimeoutStopSec=30`. Not `PartOf` the target: a target stop
  leaves a run in flight, so stop one by name with `systemctl --user stop
  holophyte-sweep.service`.

```
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp deploy/holophyte.target deploy/holophyte-serve.socket \
   deploy/holophyte-serve.service deploy/holophyte-sweep.timer \
   deploy/holophyte-sweep.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemd-analyze --user verify ~/.config/systemd/user/holophyte.target
systemctl --user enable --now holophyte.target
systemctl --user list-timers holophyte-sweep.timer
journalctl --user -u holophyte-sweep.service -u holophyte-serve.service -f
```

Each unit's `WorkingDirectory` is `%h`-relative and names one checkout
layout; adjust it before enabling if the factory lives elsewhere.

## The loop unit

- `deploy/holophyte-loop@.service` — one pass of the loop, `factory.py
  TARGET`, `Type=exec` with `Restart=no`, one instance per project named by
  its `[serve] name`. It reads `HOLOPHYTE_TARGET` from
  `~/.holophyte/NAME/serve.env`. The host sweep starts it when the project
  has a ready ticket and no loop is live, as the daemon's `launch-loop`
  action does. Start it by hand with
  `systemctl --user start holophyte-loop@NAME`: it runs the queue down and
  the unit is inactive again once the loop prints its idle line; a failed pass stays visible as a
  failed unit rather than restarting. Not meant to be enabled. Neither a
  restart nor a stop of `holophyte.target` touches a running instance.

## The project units

For a host that runs one project unregistered, or a project kept out of
`host.toml`: the project forms, one instance per project slug, each reading
`~/.holophyte/SLUG/serve.env`.

- `deploy/holophyte-serve@.service` — `factory.py TARGET --serve
  ADDRESS:PORT` (the long form of `--serve 7710`, so the address is a key of
  its own) with `Restart=on-failure`. The target path, bind address and port
  come from the environment file; the keys and the port convention are in
  `docs/operating.md` under "Serving standing".
- `deploy/holophyte-supervise@.service` — the supervisor, `factory.py TARGET
  --supervise`, with `Restart=on-failure`; only `HOLOPHYTE_TARGET` is read
  from the environment file. Enable with
  `systemctl --user enable --now holophyte-supervise@NAME`. It is refused for a project `host.toml` lists,
  which the host sweep watches: never enable it for a registered project,
  and stop it before registering one.

All of them log to the journal under their unit name
(`journalctl --user -u holophyte-sweep.service`), and none depends on a
shell session: a dead tmux server takes down nothing that runs here.

macOS writers have no systemd; a launchd equivalent is not shipped. There,
`factory.py --supervise` (no project) runs the host sweep every
`[supervisor] sweep_sec` in one process, and `factory.py --serve` binds
`host.toml`'s `[serve] bind` itself and re-executes on a `HEAD` move.
