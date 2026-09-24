# Operating

Supervising a project and serving its state. The daemon reads by default
and writes only through `[serve] actions` and `config_edit`
([The daemon's actions](reference/daemon.md)). The operator
commands (`--requeue KO-n --note TEXT`, `--file-ticket TICKET.md
[--update KO-n]`, `--approve KO-n`, `--babysit KO-n`,
`--repoint KO-n SHA`, `--pause KO-n`, `--resume KO-n`, `--abort KO-n
[--close-pr]`, `--close KO-n --landed URL`, `--hold`, `--release-hold` and
`factory.py project add|remove|list|enable|hold|disable`) are described by
`factory.py --help` and the [CLI reference](reference/cli.md), and the
escalation ladder they sit on in the [runbook](operating/runbook.md). Back
to the [README](index.md).

## Pause one run at its next safe point

`factory.py PROJECT --pause KO-n --note "reboot writer"` records an intervention
and marks that run in one transaction. The current turn continues; `/status`
reports `stop_requested` with the note, and the console run card shows
“Pause requested” until the stop takes effect. An ended run refuses the request
and names its outcome. Repeating a pending request keeps the original note.

The loop checks before each phase, after implementation, verification and
review, after a fix turn, and between babysit passes and polling steps. A pause
never freezes or kills a streaming turn. The stop stages work using the same
environment exclusions as worktree reclaim, commits remaining edits as WIP,
preserves the worktree and branch, and ends the run with outcome `paused` and
its next phase in `resumePhase`. The ticket is `blocked_on_operator` with the
request note. Babysit fixes stop before their push or thread replies; resuming
an open PR finishes a saved fix step (including its pending push and replies)
before returning through the gate to read current checks and threads.

`factory.py PROJECT --resume KO-n --note TEXT` uses the store resume path,
recording the note on the resume intervention, and returns the ticket to
ready (`POST /actions/resume` on the daemon is the same call). The next claim reuses the worktree and continues from the
recorded boundary. Implementation is skipped when it already finished; review
continuations retain the verification result and findings they need. A pause
does not grant merge approval: projects requiring a human still require it.

This change migrates the store from schema 31 to 32, adding `runs.stopRequested`
and the `paused` outcome/phase and `pause` intervention action. A pending pause
requires a writer running this build to reach a boundary.


## Abort one run now

`factory.py PROJECT --abort KO-n --note "host going down"` ends a run
immediately, and records before it acts: one transaction writes the `abort`
intervention and marks the run (the same `runs.stopRequested` a pause uses;
an abort supersedes a pending pause). `/status` reports `stop_action: "abort"`
beside `stop_requested` and the console run card shows “Abort requested” until
the run ends.

The run's worker notices the mark at its next heartbeat. It kills the current
turn's process group, the same `SIGKILL` a budget timeout sends: an
implementer's, or a configured `[agents]` reviewer's. A review on the default
container route has its container client killed, and the runner then
removes the container. It then
stages the tree with the reclaim path's environment exclusions and commits it
as `WIP: preserve work at operator abort`. When the run has a pull request it
pushes the branch. Last, it ends the run `abandoned` with the note and parks
the ticket `blocked_on_operator` with the note as its question. When the run
has no live worker, the command does the same itself, minus the kill, and
moves the board issue to the parked state and takes the lease label off, as
the worker does; so a project with no `[board]` table exits naming the key.
No live worker means the run is parked awaiting merge approval, or, on the
host that claimed it, the process recorded at claim (`runs.workerPid`) no
longer exists. A stale heartbeat alone is not enough: a slow worker, a run
claimed on another host, or one with no recorded pid may still be writing
the tree, so the abort stays pending for its worker's next heartbeat, or
for the sweep once that worker is confirmed silent. Nothing is merged and nothing is deleted: the worktree and
branch stay for the sweep's debris path, and an open pull request stays open.
An ended run, or one parked `blocked_on_operator`, refuses the request and
names why, and nothing is written.

This adds the `abort` intervention action and the `runs.workerPid` column.
The store widens its action check and adds the column in place, with no
schema version change.

`--abort KO-n --close-pr --note "wrong approach"` is the same abort when the
candidate is dead: the intervention is recorded as `abort_close` instead, in
the same transaction, before anything is killed, so whichever process
finishes the abort -- the worker or the command -- also closes the pull
request. Once the run has ended `abandoned` it posts one comment on the pull
request, under the factory's comment header, giving the note, the short sha
the branch was kept at and `--requeue KO-n` as the way to start again, and
then closes it. The branch and worktree are kept. A refused comment or close
does not undo the abort: it is a `warning` run event and a printed line. The
reconcile leaves the closed pull request alone, since the run is no longer
parked on it. `--close-pr` without `--abort` is refused. The `abort_close`
action is schema version 35 (KO-611).

## Requeue, re-point or send back a parked ticket

A ticket parked `blocked_on_operator` by a merge gate conflict -- the gate's
merge of `main` into the branch conflicted, the run failed and the branch
was preserved -- comes back through `--requeue KO-n --note TEXT` once you
have resolved the merge on the branch: the requeue is recorded, the block
cleared and the ticket walked to `ready`, and the next claim resumes on the
preserved branch as after any other failure. `--repoint` is for a candidate
still parked `awaiting_merge_approval` and rebuilt on a rewritten `main`,
not for a failed run, and not for commits pushed on top of the candidate:
`--babysit KO-n` clears the parked question as it releases the run and
readies the ticket; its next claim resumes the candidate, fetching and
fast-forwarding to those commits by itself
(see [the loop](loop.md)), since `--repoint` moves `runs.candidateSha` and
not the worktree; every other
`blocked_on_operator` park (a pull request, `merge?`, a strike-out) keeps
`--requeue`'s refusal.

A custom `--note` on `--babysit` records a maintainer instruction like the
console’s Send back, while no note or the default `sent back to the babysitter`
requests another look at the pull request.

## Supervising

The loop watches itself only while it is alive. A crashed or hung run leaves
a row in a work phase and a lease nobody gives back, and the supervisor is
what notices: an acting sweep (`--sweep --act`) that fails any run with a
dead heartbeat, a blown time box or a stuck review, releases its leases and
leaves its branch and worktree for a human. On a host with a registry the
**host sweep** is that watcher for every registered project; a project kept
out of the registry has a supervisor of its own. The two do the same work
per store and never watch one store together.

### The host sweep

`factory.py --supervise --once`, with no project, is one run over every
project in the host registry, `HOLOPHYTE_HOME/host.toml`
([below](#the-host-registry)), and an exit. On systemd the
`holophyte-sweep.timer` starts it every 60 seconds as the oneshot
`holophyte-sweep.service` ([Serving standing](#serving-standing)), so every
run is the checkout's current `HEAD` and a merge needs nothing restarted.
Without systemd, `factory.py --supervise` with no project runs the same
pass every `[supervisor] sweep_sec` of `host.toml` in one process until
SIGINT or SIGTERM; it runs the code it started with, so restart it after a
pull.

A run takes the host's lock, `HOLOPHYTE_HOME/supervisor.lock`, with the
exclusive create and dead-pid reclaim the project lock below uses: a second
run beside a live one exits 1 naming its pid, and a run killed mid-way
leaves a dead pid the next run reclaims. systemd never starts a oneshot that
is still running, so under the timer the unit itself keeps runs apart. A
run then:

1. sweeps every registered store and writes that store's beat right after
   it: one `supervisorHeartbeats` row per store, pid 0, which every reader
   prints as "host sweep". A store that cannot be opened (locked, corrupt)
   is skipped this run and swept after the others next time; three runs in
   a row mark it `unavailable`. A project whose own `supervisor.lock` names
   a live pid is skipped naming the pid and path, a dead one is removed and
   said so. A disabled project, or one whose store is missing or holds no
   row for its path, is skipped: the sweep writes no `projects` row;
   `project add` does.
2. acts on the trips and reconciles, as the project form's pass below does,
   round-robin from where the last run stopped, under one deadline of half
   the interval. Before every Linear or GitHub request the run checks the
   deadline, and a project whose share is spent is cut at its next request
   and first in line next run.

What a run must remember between runs and no store column holds lives in
`HOLOPHYTE_HOME/sweep.json`, rewritten whole after every project: when the
run started and ended, its pid, revision and exit, each project's outcome
(`ok`, `skipped: WHY` or `error: WHY`), the unavailable counts, the
round-robin cursor, the GitHub rate budget and the pull-request and board
throttles. A missing or unreadable file is an empty state and one line. A
run whose `started` has no `ended` did not finish; the next run says so and
resumes at the cursor. One project's failure is that project's `error`,
never the run's; the run exits 1 when any project errored, and the timer
fires again at the next interval regardless.

A registered project is the host sweep's alone. The loop's
`[loop] spawn_supervisor` spawns nothing for it and prints `the host sweep
watches PATH`, and `factory.py PROJECT --supervise` for it is refused
naming `host.toml`.

### A project's own supervisor

For a project not in the registry (a one-project host, a test):

```
python3 factory.py --supervise /path/to/repo
```

It runs the pass below every `[supervisor] sweep_interval_sec` (60 s by
default) as a long-lived process. Running it by hand is optional: the loop
starts one itself. At startup,
after its config and route checks and before its first claim, the loop reads
the project's `supervisor.lock`, and when no live pid holds it spawns
`factory.py --supervise` for the same project in its own session, with stdout
and stderr appended to `supervisor.log` in the state directory, and prints
`[holo2] started a supervisor for PROJECT as pid N`; when a live supervisor
already holds the lock it prints `[holo2] supervisor pid N is watching
PROJECT` and carries on. The spawned supervisor outlives the loop on purpose
and takes the lock itself, so two loops starting at once resolve at the lock
like two `--supervise`s. `[loop] spawn_supervisor = false` turns the spawn
off for an operator whose service manager runs the supervisor
([Config](config.md)).

It runs until SIGINT or SIGTERM, finishing the pass in hand and exiting
clean. One supervisor per project: the first takes
`supervisor.lock` in the project's state directory (beside the store) with an
exclusive create and writes its pid into it; a second `--supervise` for the
same project exits non-zero naming that pid. A lock whose pid is dead is a
supervisor that was killed without the chance to clean up, and is reclaimed
on the next start; reclaims take turns under an flock on the sidecar
`supervisor.lock.reclaim` beside it, which is left in place. A lock
that names no pid at all is not guessed about: the start refuses and says
which file to look at. It runs the code it started with: a store stamped by
a newer build ends it, and its service manager (`Restart=on-failure`) starts
it again on the new code.

### What a pass does

Each pass bumps the watcher's row in the store's `supervisorHeartbeats`
table, so whether the watcher is still watching is a query rather than a
`ps`. Each pass also reconciles parked pull requests whenever no loop is
live on the project: a run parked on its pull request (`[merge] mode =
"pr"`) is closed out as merged, with the merge commit's sha, within one
sweep interval of a person merging it on GitHub, exactly as the loop's own
pass would have done had it still been running; while a loop's heartbeat
is fresh the loop's tick does that and the supervisor leaves it alone. A
GitHub error there is one printed line and never a strike. Every pass
ends by starting the loop whenever a ticket is ready and no loop is
running, however the ticket got there -- that reconcile sending a run
back to the babysitter, an operator's `--requeue` or `--babysit`, a
ticket filed while the loop was down -- the way the console's
launch-loop action does,
`systemctl --user start holophyte-loop@NAME` with `[serve] name`, and
prints that it did. "No loop live" is no fresh heartbeat and nobody
holding the project's `lease.lock` turn. The attempt is recorded on the
ticket's newest run when it has one (a `launch_loop_attempt` event)
before `systemctl` is
asked, and a start it took is recorded after as a `launch_loop`
intervention; a
`systemctl` that fails is printed, records a `launch_loop_failed` event
and no intervention. The records are the story, not a discharge: the
next pass tries again while the ticket is still `ready`
and the loop still free -- a taken start whose loop never came live is
owed again -- so on a host without the units the line repeats
until the operator's launcher takes the ticket. The store's `ready`
rows are a mirror a loop pass wrote, so a ticket that became ready
while no loop ran -- filed with `--file-ticket`, moved from Backlog to
Todo -- has no row to find; a pass whose mirror answer is empty then
asks the board itself, the same ready read the loop claims from, and a
filed ticket starts the loop by itself. The ask is one board query a
pass and only on the miss; a board that cannot be asked is one printed
line and the next pass asks again. The sweep also
watches the loop's own restarts: a loop that merges a change to the
factory itself writes a `loopRestarts` row and re-executes,
and if no claim, heartbeat or "no ready tickets" exit follows within
`restart_grace_sec` the next sweep prints `loop did not return after re-exec
from <sha>` and records it, once per restart. Nothing else is relaunched.
Process management (systemd, a tmux pane, `nohup`) is the operator's;
the factory ships the invocation and nothing around it.

## Serving

`--serve` runs an HTTP daemon, so a drawer or dashboard can poll the
factory over HTTP instead of reading the store. It reads by default; the
project actions (`POST .../actions/...`) and `[serve] config_edit`
(`PUT .../config`) are the two opt-ins that make it write
([The daemon's actions](reference/daemon.md)).

`factory.py --serve`, with no project, is the **host daemon**: one daemon
for every project in the host registry. Each project's routes answer under
`/projects/NAME/...`, `NAME` its `[serve] name`, with the bodies a project
daemon answers at its root; the root's `/status` lists every project with
the builds and the last host sweep, and the root's `/attention` merges
every project's items, each carrying its `project`
([HTTP endpoints](reference/http.md#the-host-daemon)). A name resolves
through the registry alone, and a name outside it is 404 before any file
is opened; `project add` and `project remove` take effect at the next
request, with no restart. One project's store locked, missing or stamped
by a newer build this one cannot read is that project's 503 and its `error` on the root
`/status`; the other projects answer whole. Under systemd the daemon is
socket-activated ([Serving standing](#serving-standing)); by hand it binds
the address given, else `host.toml`'s `[serve] bind`:

```
python3 factory.py --serve 7710
```

`factory.py --serve 7710 /path/to/repo`, with a project, is the **project
daemon**, the same routes at its root for that project alone: the form for
a project kept out of the registry and for tests.

A bare port binds `127.0.0.1`, which is the whole setup on one machine.
When the drawer runs on another machine, give the host too:
`--serve HOST:PORT` binds this host's address on the private network
between them ([Across machines](operating/hosts.md)).

It serves the console page at `/` and the console's built files under
it, and answers the JSON routes listed in [HTTP endpoints](reference/http.md);
that page holds the bodies and status codes and is not repeated here.
Every response is `Cache-Control: no-store` and
`Access-Control-Allow-Origin: *`, and every GET opens the store
through a read-only connection and closes it; the daemon never holds a
connection between requests and writes only through the two opt-ins.
Any other path is 404 as JSON. The console page polls its peer daemons from the browser, so every
daemon answers the browser's CORS preflight, `OPTIONS` on any path, with
204 and no body, token or not; every other method but GET stays 405, as
JSON, save the two opt-ins' writing routes.

Every `host` passes through `[report] host_label`, so a configured label is
what the network sees rather than the machine name.

The run record is the store, read through the console or `--report`; no
project renders it into a `FINDINGS.md` unless its config says
`[report] findings = "repo"` ([Configuration](config.md)).

On loopback the boundary is the bind address and nothing else: the
daemon binds the one address it is given (loopback when it is only a port)
and anyone who can reach that port can read run and ticket
identifiers, phases, heartbeat ages and the estimate-vs-actual history. Keep
the bare port unless another machine must reach it, and then name one
private-network address and a token: `host.toml`'s `[serve]
machine_token_file` for the host daemon, a project's `[serve] token_file`
for a project daemon. Beyond loopback the daemon refuses to start without
it and answers 401 to every JSON request that does not present the file's
contents as `Authorization: Bearer TOKEN`; only `/peers`, the console page
and its files stay open, so the page can load and find its peers before it
has a token to present ([Across machines](operating/hosts.md),
[config](config.md)). The token is a second boundary, not a substitute for
the first: binding the wildcard address (all interfaces) still offers the
port to every network the host is on.


## Serving standing

A daemon or sweep started by hand in a tmux session ends silently at the
next reboot, and the drawer then reads the host as "attention needed".
`deploy/` ships systemd user units that keep them standing, provided the
operator's user manager itself starts at boot (lingering, below).

On a host with a registry, five host units, none reading an environment
file (`deploy/README.md` has each unit's keys):

| Unit | Does |
| --- | --- |
| `holophyte.target` | `Wants=` the socket and the timer; the only one to enable |
| `holophyte-serve.socket` | holds `127.0.0.1:7710` and hands it to the daemon; connections queue on it while no daemon runs |
| `holophyte-serve.service` | `factory.py --serve`, started by the socket on the first connection; on a factory `HEAD` move it drains for at most 20 s and exits 0, and the next connection starts the new code; `Restart=on-failure` every 5 s with no start limit, so a daemon that cannot start leaves the port held |
| `holophyte-sweep.timer` | starts the sweep 5 s after it starts and every 60 s after, start to start; a run past 60 s skips the next fire, never overlaps it |
| `holophyte-sweep.service` | `factory.py --supervise --once`, a oneshot; `TimeoutStartSec=120`, above the 97 s a single Linear read can take |

The socket's address and `host.toml`'s `[serve] bind` are typed in two
places: keep them equal (the daemon names a difference once and serves the
socket's). The timer's 60 s and `host.toml`'s `[supervisor] sweep_sec` are
too: the daemon calls the sweep stale after two `sweep_sec` intervals.

```
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp deploy/holophyte.target deploy/holophyte-serve.socket deploy/holophyte-serve.service \
   deploy/holophyte-sweep.timer deploy/holophyte-sweep.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now holophyte.target
systemctl --user list-timers holophyte-sweep.timer
journalctl --user -u holophyte-sweep.service -u holophyte-serve.service -f
```

`systemctl --user restart holophyte.target` restarts the socket, the daemon
and the timer, and `stop` stops all three. Neither touches a sweep run in
flight (the sweep service is not `PartOf=holophyte.target`: stop it by
name, `systemctl --user stop holophyte-sweep.service`) or a running loop.

The first line matters for an unattended reboot: a user unit is run by the
operator's user manager, and without lingering that manager only starts when
the operator logs in, so an enabled unit would wait for a login that never
comes on a headless host. `loginctl enable-linger` starts the user
manager at boot; run it once per host, and check with
`loginctl show-user "$USER" -p Linger` (expect `Linger=yes`).

Each unit's `WorkingDirectory` is `%h`-relative and names one checkout
layout; adjust it before enabling if the factory lives elsewhere.

The loop stays per project: `deploy/holophyte-loop@.service`, one pass,
instance `NAME` its `[serve] name`, started by the sweep or the console's
launch-loop action. It reads `HOLOPHYTE_TARGET` from
`~/.holophyte/NAME/serve.env`, so a registered project still needs that
file, with that one key.

### The project units

For a project kept out of the registry, `deploy/holophyte-serve@.service`
keeps one project daemon standing, the instance name the project slug, and
`deploy/holophyte-supervise@.service` its supervisor. Never enable the
supervisor unit for a registered project: it is refused, and restarts into
the refusal. The serve unit reads three keys from
`~/.holophyte/SLUG/serve.env`:

| Key | Value |
| --- | --- |
| `HOLOPHYTE_TARGET` | the project repository path |
| `HOLOPHYTE_SERVE_ADDRESS` | `127.0.0.1` (what `--serve PORT` binds), or the host's private-network address |
| `HOLOPHYTE_SERVE_PORT` | the port from the convention below |

The address is `127.0.0.1` on one machine, or the host's address on the
private network a remote drawer uses; never the wildcard address (see
"Serving" above for what an open bind publishes).

**Port convention:** 7710 is the host daemon's. A host that runs project
daemons instead gives the first 7710 and counts up by one per further
project, so a client config is two lines per project: a host serving
`holophyte` and `lotuspod` has them on 7710 and 7711.

An example `~/.holophyte/holophyte/serve.env`:

```
HOLOPHYTE_TARGET=/path/to/holophyte
HOLOPHYTE_SERVE_ADDRESS=127.0.0.1
HOLOPHYTE_SERVE_PORT=7710
```

Install and enable, one instance per project:

```
mkdir -p ~/.config/systemd/user && cp deploy/holophyte-serve@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now holophyte-serve@holophyte
journalctl --user -u holophyte-serve@holophyte -f
```

The project daemon re-executes itself when the factory checkout's `HEAD`
moves; it reads the store per request and has no state to lose. A client
finds it at the bind address and the project's port from the convention,
nothing else; splitting the drawer onto a second machine is
[Across machines](operating/hosts.md).

### The host registry

`HOLOPHYTE_HOME/host.toml` (default `~/.holophyte/host.toml`) lists the
projects the host daemon serves and the host sweep watches, by path, and
the host's own keys. `project add` and `project remove` write it, whole,
through an exclusive `host.toml.tmp` and a rename; edit the host's keys by
hand. A key outside this list is refused, as a project config refuses one.

```toml
[serve]
# What `factory.py --serve` binds without a socket from the service manager
# or an address on its command line. Keep it equal to the socket unit's
# ListenStream.
bind = "127.0.0.1:7710"
# The one bearer token of the host daemon, at the root and under every
# project prefix. Required beyond loopback, with `actions`, or when any
# project sets `[serve] config_edit`. A relative path is taken against the
# home. Held to `[serve] token_file`'s rules (a chmod 600, non-empty file).
machine_token_file = "machine.token"
# Open each project's actions under /projects/NAME/actions/... and, at the
# root, POST /actions/run-sweep. Off: every actions path is 404.
actions = false

[supervisor]
# The host sweep's interval, in seconds: the `--supervise` loop's sleep and
# the unit of the daemon's stale rule. Equal to the timer's OnUnitActiveSec.
sweep_sec = 60

[console]
# Other hosts' daemons the console fans out to, as HOST:PORT strings.
daemons = []

[[project]]
path = "/path/to/holophyte"
```

Everything else about a project stays in its own `config.toml`: its route
and unit name (`[serve] name`), its thresholds, `config_edit`, its
`token_file`. On a host daemon a project's own `bind`, `actions`,
`machine_token_file` and `[console] daemons` are not read; a project whose
`machine_token_file` differs from the host's is named once at start. A
project's `token_file` is still accepted under that project's own prefix,
for one release. Two entries that resolve to one name or one path refuse
the registry, naming both; an entry whose config cannot be read is listed
with its error and served to no one. The host daemon and the sweep re-read
the `[[project]]` list whenever the file changes; the daemon reads the
`[serve]` and `[console]` keys once, at start.

Beside it in the home: `supervisor.lock`, the host sweep's lock;
`sweep.json`, its record; and `host-actions.jsonl`, the host daemon's
ledger, one JSON line per `POST /actions/run-sweep`, written before
`systemctl` is asked.

### Registering and disabling projects

`python3 factory.py project add PATH` validates a repository root and its
existing `[board]` configuration, then registers it without starting a run,
in its store and in the host registry, `HOLOPHYTE_HOME/host.toml`. A second
add of the same path, or of a project whose `[serve] name` is already
registered, refuses and names the entry; a store row the loop wrote for the
same team and path is adopted rather than refused. A registered path whose
store has lost its row (deleted or recreated after registration) is the one
exception: `project add PATH` writes the row back and leaves `host.toml`
unchanged. `project remove NAME` drops a registry entry and leaves its store
alone; it takes the entry's path as well, which is how an entry whose config
no longer loads, or one sharing a name with another, is removed. Configuration remains in its
existing per-project file; registration does not move it. New registrations,
including implicit loop registration, store canonical absolute repository paths.
If a legacy row has a relative path, registration and admission checks refuse
with its project ID and a request for operator repair. Its original base is not
stored: verify the original repository location and repair the row to its
canonical absolute path through the operator protocol before retrying. Do not
interpret it relative to the current working directory. This refusal applies
across the store because the ambiguous row could identify any project.
`project list` remains available to inspect the rows.

Registering a project hands its watch to the host sweep: stop and disable
its `holophyte-supervise@` unit first, since a registered project's
`--supervise` is refused.

`python3 factory.py project list` prints each registered project's name,
path, admission and note from its own store; `python3 factory.py --status`
with no project reports every one of them. With `--store PATH`, `project list`
prints that store's rows instead: name, path, admission, note and newest run,
ordered by name and path. The admission commands accept `--store PATH` to
select one database explicitly; this does not combine stores. Without it, `add`
uses the added repository's store and the other commands use the current
repository's store. `add --store` naming any other database registers there
only and leaves `host.toml` alone: the registry holds paths, and the host
reads each project's own store.

`project hold NAME --note TEXT` stops new admission while workers drain.
`project disable NAME --note TEXT` also stops admission; the host sweep
skips a disabled project, a project's own supervisor exits at startup, and
`/status` reports the disabled state and note with no runs.
`project enable NAME` enables admission again (an optional `--note` records why).
NAME is the repository directory's basename; ambiguous names are refused.
The existing `factory.py PATH --hold --note TEXT` and
`factory.py PATH --release-hold --note TEXT` remain aliases with the same
`hold` and `release_hold` intervention actions. Disabling records `disable`, and
registration records `register_project`.

This change upgrades the store schema from 29 to 30 to widen the project
admission CHECK to include `disabled` and admit the new intervention actions.
