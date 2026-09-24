# Processes

Six kinds of process touch a project. The loop is long-lived and one per
project; the sweep and the daemon are one per host, for every project in the
host registry (`HOLOPHYTE_HOME/host.toml`); two are spawned per run; one
polls the daemon over HTTP.

## The loop

`python3 factory.py /path/to/repo`

One process per project. Claims, works and closes out one ticket at a time,
then claims the next; exits when the board has no ready ticket, when a run
fails (`[loop] stop_on_failure`, the default), or when it merges a change to
the factory itself, in which case it re-executes `factory.py` from the new
`main` and carries on. It is the only writer of `runs.phase` (through
`runs.set_phase()`) and of `main`.

At startup it validates every config table, live-probes the configured
agent routes, runs a read-only sweep and prints it, and refuses to start
without a `[board]` table. It holds one write connection to the store for
its lifetime; the heartbeat thread opens its own.

A run's phases in order: `claimed → working → verifying → reviewing →
(addressing → verifying → reviewing) → merge_gate → merging → done`, or
`failed` from any of them. The store refuses any edge the diagram in
[The loop](../loop.md#state-machines) does not draw, and `set_phase()` on a
run the supervisor already ended raises `RunEnded`, which is how the loop
stops cleanly instead of advancing a corpse.

## The supervisor

`python3 factory.py --supervise --once` (the host sweep; no project)

One run over every registered store, started by `holophyte-sweep.timer`
every 60 s as a oneshot, and an exit; systemd never starts one while
another is running, and each run holds `supervisor.lock` in the host's home.
A run sweeps every store: every live run is sighted; a heartbeat older than
`heartbeat_stale_min` counts a strike, and `stale_strikes` consecutive
strikes end the run; a run past its time box plus grace ends; two review
rounds whose findings overlap at or above `review_overlap_threshold` end
the run as `review_stuck`. Ending a run means `store.release()` with the
leases freed and the branch kept. It also watches for a loop that
re-executed and never came back, reconciles parked pull requests and board
closes, and starts a project's loop unit when a ticket is ready and no loop
is live; that part, the network part, runs round-robin under a deadline of
half the interval.

Each run bumps one `supervisorHeartbeats` row per store, pid 0, so "is the
watcher watching" is a query, and writes `sweep.json` in the host's home
after every project: what it must remember between runs (the round-robin
cursor, the GitHub budget, the throttles) and where it stopped. Every run
is the checkout's `HEAD`, so a self-merge needs nothing restarted. One
store locked, missing or disabled is that project's outcome in
`sweep.json`, never the run's.

`python3 factory.py --supervise /path/to/repo` is the same pass for one
project kept out of the registry, every `[supervisor] sweep_interval_sec`
as a long-lived process, one per project, enforced by `supervisor.lock` in
the project's state directory; the loop starts it when none is live. It
runs the code it started with and ends when a newer build stamps its store,
for its service manager to start again.

It is deliberately dumb about the work: it reads the store, never touches a
worktree, and talks to Linear and GitHub only to post the comment a swept
run gets, close out a pull request a person merged and ask the board what
is ready.

## The serve daemon

`python3 factory.py --serve` (the host daemon; no project)

One per host, socket-activated: `holophyte-serve.socket` holds port 7710
and starts `holophyte-serve.service` on the first connection, handing it the
listening socket. A `ThreadingHTTPServer`; every project's routes answer
under `/projects/NAME`, and the root answers the host's `/status` and
`/attention`. Every request opens the project's store read-only, answers,
closes; one store's failure is that project's 503, never the daemon's. Its
read routes go through `store.read`; its `POST .../actions/...` endpoints
write through the store API (`store.record_intervention()`,
`store.requeue()`, `store.operator_notes.send_back()`), recording before
they requeue a ticket, act on a unit or send a run back, and its root
`POST /actions/run-sweep` records in the host's `host-actions.jsonl` before
it starts the sweep. Endpoints in the [HTTP reference](../reference/http.md).
On loopback the bind address is the boundary; beyond it the host's machine
token guards every JSON route but `/peers`. Stateless: when the factory
checkout's `HEAD` moves it drains its requests and exits, and the socket
starts the new code on the next request.

`python3 factory.py --serve 7710 /path/to/repo` is the project daemon, the
same routes at its root for one project; it binds its own port and
re-executes itself on a `HEAD` move.

## The implementer

Spawned by the loop per run: the `[agents] implementer` command (default
`claude -p`) with the ticket body as its prompt, in its own process group,
in the worktree, with a wall-clock budget. Killed as a group on budget. It
sees the repository and the ticket; it does not see the store, Linear, or
the reviewer's output except as text the loop feeds back in a fix round.

## The reviewer

Spawned by the loop per round: `docker run` of the pinned reviewer image
with a staged export of the candidate mounted read-only at `/workspace`,
a disposable copy of the Codex auth, no host home, no Docker socket, no
capabilities. Codex inside gets the ticket, the verify report and the
criteria checklist instruction. The container is removed when the review
ends, when the loop takes SIGTERM, or by `--sweep --act` if it outlived its
loop. The same image and prompt shape serve the terminal adjudicator.
[Reviewing](../reviewing.md) has the boundary in full.

## The drawer

`contrib/swiftbar/holophyte.10s.py`, run by SwiftBar on the operator's
Mac every ten seconds. Reads `~/.holophyte/drawer.toml` for one daemon per
host (or per project, for a project daemon), fetches each daemon's JSON
with a two-second timeout (a host daemon's root, then each project under
its prefix), and prints a menu: a "needs you" section when anything needs
the operator, a line per host for its last sweep, then one block per
project. The glyph is the two-leaf mark; a green, amber or red dot
inside it is the worst level across daemons. It has no state and no write
path.

## How they restart

| Process | Restarts itself when | Restarted by hand when |
| --- | --- | --- |
| loop | it merges a factory change (re-exec) | queue was empty and new tickets are filed; after a failed run |
| host sweep | never: every run is a new process at the checkout's `HEAD` | never; `systemctl --user stop holophyte-sweep.service` stops a run in flight |
| host daemon | the factory checkout's `HEAD` moves: it exits and the socket starts the new code; `Restart=on-failure` in the unit | after a renderer merge, once the console bundle is rebuilt, since the daemon serves it from `console/dist/` |
| project supervisor | never; a newer schema ends it for its service manager | after a pull, when run by hand |
| project daemon | the factory checkout's `HEAD` moves (re-exec on its own bind) | after a renderer merge, as the host daemon |
| drawer | every 10 s by SwiftBar | after pulling a new script version (SwiftBar refresh) |

The loop and the project daemon both go through `holophyte/reexec.py`,
which replaces the process image with the same command line through an
injectable `EXEC` seam so tests can watch it happen without exec'ing.
