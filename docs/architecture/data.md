# Store and state

The store is the source of truth. Linear, `FINDINGS.md`, the daemon's JSON
and the drawer are views of it; the loop and the supervisor are its only
writers. It is one SQLite file per target in WAL mode, at
`~/.holophyte/<slug>/store.db`, with a versioned schema
(`PRAGMA user_version`, currently 3) and forward-only migrations. A build
that opens a store stamped newer than it understands refuses and exits.

## Tables

| Table | One row per | Written by | Notes |
| --- | --- | --- | --- |
| `projects` | target | loop | `activeRunId` is the lease: one run per project at a time |
| `tickets` | Linear issue the loop has mirrored | loop | status machine below; `blockedQuestion` when parked for a human; the contract snapshot the merge gate compares against |
| `runs` | attempt at a ticket | loop, supervisor (end only) | phase machine below; `lastHeartbeat`, `timeBoxMs`, `outcome`, `outcomeReason`, `outcomeClass` (`work` or `infra`), `resumePhase`, `host` |
| `reviewRounds` | review or adjudication round | loop | verdict, structured findings, their fingerprint, the verify result shown to the reviewer, the agent route |
| `runEvents` | narrative event | loop, supervisor | phase changes, warnings, sweeps; the story `FINDINGS.md` does not tell |
| `sweepStrikes` | supervisor sighting | supervisor | consecutive silent sightings per run |
| `supervisorHeartbeats` | supervisor process | supervisor | pid, start, last beat, passes, host |
| `loopRestarts` | self-merge re-exec | loop | sha; the supervisor checks the loop came back |
| `linearDeliveries` | push to Linear | loop | what was projected, when |
| `interventions` | operator or supervisor decision on a run | operator commands, supervisor | action ∈ `redirect, kill, extend_time_box, resume, close_out, requeue`; the record-before-acting rule lives here |

## The two state machines

Both live as data (`TICKET_TRANSITIONS`, `RUN_PHASE_TRANSITIONS` in
`store/__init__.py`), `store.transition()` refuses any edge not in them,
and a test fails if the rendered diagram in [The loop](../loop.md) drifts
from the tables.

**Ticket status** answers "can this be claimed?": `ready` is claimable;
`in_flight` has or had a run; `merged` and `abandoned` are terminal;
`needs_spec` failed the template validator; `blocked_on_deps` waits on a
Linear `blocks` relation; `blocked_on_operator` waits on a human and
carries a `blockedQuestion`. A failed run leaves the ticket `in_flight`
with no active run, which `pickable()` refuses; `--requeue` walks it back
to `ready` along the legal edges and records why.

**Run phase** answers "what is this attempt doing?" and is written only by
`runs.set_phase()`, which also moves the heartbeat and appends an event in
the same transaction. `failed`, `killed` and `done` are terminal;
`set_phase()` on an ended run raises `RunEnded` so a loop that lost its run
to the supervisor stops instead of advancing it.

## Reads

`store/read.py` holds the typed read views: one query, one frozen
dataclass, no SQL anywhere else. `open_readonly()` opens the file with
`mode=ro` so a reader can never take the write lock. `Ticket`,
`RunSnapshot`, `LiveRun`, `EndedRun`, `ReviewRound`, `Strike`,
`SupervisorBeat` and the functions that return them are pinned by an
allow-list in `tests/test_store_surface.py`, so a new read is a deliberate
addition. The serve daemon, `--report`, the sweep and the FINDINGS renderer
all read through them, which is why the terminal, the JSON and the file
never disagree.

## Leases and locks

- `projects.activeRunId` is the claim lease, taken and released under
  `BEGIN IMMEDIATE`. A loop that dies holding it blocks every later claim
  until the supervisor sweeps the run, which is the supervisor's reason to
  exist.
- `supervisor.lock` in the state directory holds the supervisor's pid;
  a second supervisor for the same target exits naming it. A dead pid is
  reclaimed under an `flock` on a sidecar.
- Review scratch directories under `~/.cache/holophyte/reviews/` are
  temporary; a review container whose directory is gone is a stray.

## Interventions

Every out-of-band change to a run has a row here before the change, with an
action from the fixed set and a narrative event carrying the note. The
operator commands (`--requeue`) write it; the REPL rung of the escalation
ladder calls `store.record_intervention()` directly. Backdating or
mislabelling a row is worse than no row; the [runbook](../operating/runbook.md)
says why.

## FINDINGS

`FINDINGS.md` in the target repository is a rendered window over the store:
above a `<!-- store-rendered below -->` marker, frozen pre-store history
that is never rewritten; below it, the newest twenty-five run and round
entries rendered from `runs` and `reviewRounds` at every close-out, with
everything older counted in one archive line. Each merged run's entry
carries the byte-stable timing line (`actual · estimate · rounds`) that
`--report` computes from the same columns. It is committed by the loop as
part of the merge, so a public repository's evidence is exactly what the
store holds and nothing an implementer typed.
