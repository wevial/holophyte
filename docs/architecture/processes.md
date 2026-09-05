# Processes

Six kinds of process touch a target. Three are long-lived and live beside
the store; two are spawned per run; one polls the daemon over HTTP.

## The loop

`python3 factory.py /path/to/repo`

One process per target. Claims, works and closes out one ticket at a time,
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

`python3 factory.py --supervise /path/to/repo`

One per target, enforced by `supervisor.lock` in the state directory. Every
`[supervisor] sweep_interval_sec` (60 s) it runs an acting sweep: every
live run is sighted; a heartbeat older than `heartbeat_stale_min` counts a
strike, and `stale_strikes` consecutive strikes end the run; a run past its
time box plus grace ends; two review rounds whose findings overlap at or
above `review_overlap_threshold` end the run as `review_stuck`. Ending a
run means `store.release()` with the leases freed and the branch kept.
It also watches for a loop that re-executed and never came back, and for
stray review containers whose scratch directory is gone.

Each pass writes a `supervisorHeartbeats` row, so "is the watcher watching"
is a query. Before each pass it compares the factory checkout's HEAD with
the one it started on and re-executes itself when they differ, or when the
store is stamped with a schema newer than its build; this is what keeps a
self-merge from silently ending supervision.

It is deliberately dumb about the work: it reads the store and nothing
else, never talks to Linear except to post the one comment a swept run
gets, and never touches a worktree.

## The serve daemon

`python3 factory.py --serve 7710 /path/to/repo` (loopback; `HOST:PORT` to bind elsewhere)

One per target, as a systemd user unit (`holophyte-serve@SLUG`). A
`ThreadingHTTPServer` bound to the one address given, loopback when only
a port is; every request opens
the store read-only, answers, closes. It imports `store.read` and nothing
from `store`, so it cannot write. Endpoints in the
[HTTP reference](../reference/http.md). It has no authentication; the bind
address is the boundary. Stateless, so a code change is picked up by
restarting the unit.

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
target, fetches each daemon's JSON with a two-second timeout, and prints a
menu: a "needs you" section when anything needs the operator, then one
block per target. The glyph is the two-leaf mark; a green, amber or red dot
inside it is the worst level across daemons. It has no state and no write
path.

## How they restart

| Process | Restarts itself when | Restarted by hand when |
| --- | --- | --- |
| loop | it merges a factory change (re-exec) | queue was empty and new tickets are filed; after a failed run |
| supervisor | the factory checkout's HEAD moves; the store schema is newer | never, in normal operation |
| serve daemon | `Restart=on-failure` in the unit | after a merge that touches `serve.py`, `report.py` or `store/read.py` |
| drawer | every 10 s by SwiftBar | after pulling a new script version (SwiftBar refresh) |

The loop and supervisor both go through `holophyte/reexec.py`, which
replaces the process image with the same command line through an
injectable `EXEC` seam so tests can watch it happen without exec'ing.
