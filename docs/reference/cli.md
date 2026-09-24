# CLI

`python3 factory.py [MODE] PROJECT`. The command line is parsed, not
indexed, so `--help` is safe. Modes are mutually exclusive; the project is
always the repository path, except in the [host forms](#host-forms). `python3 factory.py project VERB` is a separate
command family, [below](#project-commands).

| Invocation | Does | Touches |
| --- | --- | --- |
| `factory.py PROJECT` | runs the loop: claim, work, verify, review, merge, repeat; exits on an empty board or a failed run; re-execs after a self-merge. Under `[loop] workers > 1` it is the scheduler of a pool of `--worker` children instead (see [The loop](../loop.md#the-pool)) | Linear, store, worktrees, `main`, `FINDINGS.md` |
| `--report PROJECT` | prints the estimate-vs-actual table and the supervisor liveness line, then a `failures KIND: N` line per failure kind among the failed runs (`unclassified` for one with no kind) | store, read-only |
| `--sweep PROJECT` | prints what an acting sweep would do; lists stray review containers; a `failures KIND: N` line per failure kind among the failed runs | store (sightings only) |
| `--sweep --act PROJECT` | fails tripped runs, releases their leases, removes stray containers; prints the same `failures KIND: N` lines | store, Docker |
| `--status [--json] PROJECT` | prints what the factory is doing now: projects, live and parked runs, ready tickets, schema, lock holders; `--json` prints it as one JSON object | store, read-only |
| `--import-store PATH --dry-run PROJECT` | opens the store at `PATH` and the project's own read-only and prints, per table, the rows an import would move, their id range, the offset a remap would add and a sha256 of the rows; refuses stores at different schema versions; `--dry-run` is required | store, read-only |
| `--supervise PROJECT` | the acting sweep every `sweep_interval_sec`, under the project's supervisor lock; re-execs itself when the factory code moves | store |
| `--serve PORT PROJECT` | the JSON daemon on loopback (`--serve 7710` binds `127.0.0.1:7710`), which also serves the console at `/` from the built bundle; it reads by default and writes only through two opt-ins, `[serve] actions` (`POST /actions/...`) and `[serve] config_edit` (`PUT /config`) ([The daemon's actions](daemon.md)); `--serve HOST:PORT` binds the named address instead, and a non-loopback bind demands `[serve] token_file`, whose contents every JSON request but `/peers` must present as a bearer token (`/`, the console's files and `/peers` stay open; a loopback bind, `127.0.0.1:PORT` included, ignores the key for reads, but either write opt-in demands `[serve] token_file` on every bind, loopback included, and the routes it opens answer only to the bearer) | store, read-only by default; with `[serve] actions` the store and the systemd units, with `[serve] config_edit` the project's `config.toml` |
| `--requeue KO-n --note TEXT PROJECT` | walks a failed ticket back to `ready` with an `interventions` row | store |
| `--approve KO-n [--note TEXT] PROJECT` | releases a ticket parked by `[merge] approve = "human"`: an `interventions` row with action `approve`, the parked run ended with its resume point at the merge gate, the ticket walked to `ready`; the loop's next claim reuses the preserved worktree and branch, re-runs the pre-merge verify and merges with no implementer or reviewer -- under `[merge] mode = "pr"`, babysits the pull request once more and merges it through the API when green and quiet; refuses any other state, naming it | store |
| `--babysit KO-n [--note TEXT] PROJECT` | sends a ticket parked on its pull request (`[merge] mode = "pr"`) back to the babysitter: the `interventions` row `store.babysit()` writes, the parked run ended with its resume point at the merge gate, the ticket walked to `ready`; the loop's next claim resumes the candidate on the PR and reads its threads and checks again, parking again under `approve = "human"` rather than merging; refuses any other state, naming it | store |
| `--repoint KO-n SHA --note TEXT PROJECT` | moves a parked candidate to a rebuilt branch tip: an `interventions` row with action `repoint` carrying the note, a `runEvents` row naming the old and new shas, then `runs.candidateSha` set to `SHA` (a full 40-hex commit id); the run stays parked and the branch is not touched; the merge gate `--approve` resumes into holds the branch to the new sha; refuses a ticket not parked awaiting merge approval, one already approved (its release is in flight: requeue instead) or a malformed sha, naming it | store |
| `--pause KO-n --note TEXT PROJECT` | asks the ticket's run to stop at its next safe point: a `pause` intervention carrying the note and the run marked, in one transaction; the run later commits its work as WIP, keeps its worktree and branch, ends `paused` and parks the ticket `blocked_on_operator` (see [Operating](../operating.md#pause-one-run-at-its-next-safe-point)); refuses a ticket with no run or a run already ended, naming its outcome; repeating a pending request keeps the first note | store |
| `--resume KO-n --note TEXT PROJECT` | releases a paused run to claim: the resume intervention carrying the note, the run released with its resume phase, the ticket walked to `ready` and its question cleared, then the pull request's pause notice removed; the next claim reuses the worktree and continues from the recorded boundary; refuses a ticket whose latest run did not end `paused` | store |
| `--abort KO-n --note TEXT PROJECT` | ends a run now: an `abort` intervention and the run marked in one transaction; the run's worker, or the command itself when no worker is live, kills the turn, commits the tree as WIP, pushes an open pull request's branch, ends the run `abandoned` and parks the ticket `blocked_on_operator` (see [Operating](../operating.md#abort-one-run-now)); refuses an ended run or one parked `blocked_on_operator` | store, worktree, Linear |
| `--abort KO-n --close-pr --note TEXT PROJECT` | the same abort recorded as `abort_close`; once the run has ended it comments the note on the pull request and closes it, keeping the branch | store, worktree, Linear, GitHub |
| `--hold --note TEXT PROJECT` | holds the project's admission: a `hold` intervention carrying the note; no new ticket is claimed while existing runs finish; creates the project's store row from `[board]` when it has none; refuses a project already held, or one with neither a row nor `[board]` | store |
| `--release-hold --note TEXT PROJECT` | enables admission again: a `release_hold` intervention carrying the note; refuses a project already enabled, or one with neither a row nor `[board]` | store |
| `--close KO-n --landed URL [--note TEXT] PROJECT` | closes a ticket whose change landed outside the factory: a `close_out` intervention on its last run naming `URL` and the note, the question cleared and the ticket walked to `merged` with no merge sha, in one transaction; then the lease label removed, the board issue moved and the ledger posted as a comment; refuses a ticket already merged, one with a live run, or one whose last run did not end `rejected`, `failed`, `abandoned` or `killed` | store, Linear |
| `--file-ticket TICKET.md [--state Todo\|Backlog] [--priority urgent\|high\|medium\|low] PROJECT` | validates, creates the issue in the Linear project the project's `[board]` names, reads it back, validates again | Linear |
| `--worker PROJECT` | internal, spawned by the scheduler under `[loop] workers > 1`: claims one ticket, works it to merge or park, exits with the run's status (0 merged, 1 failed, 2 parked, 3 nothing to claim, 4 stopped for a human); skips the startup probes, the sweep and the supervisor spawn, which the scheduler ran for the pool | Linear, store, worktrees, `main`, `FINDINGS.md` |
| `--file-ticket TICKET.md --update KO-n PROJECT` | same, replacing an existing issue's title, body and estimate; a blocker the file's `Depends on:` names and the board lacks is recorded and printed as `+KO-a`, one the board holds and the file no longer names is printed as `board also holds KO-c` -- relations are added, never removed | Linear |

## Project commands

`python3 factory.py project VERB [--store PATH]` registers projects in the
host registry, `HOLOPHYTE_HOME/host.toml`, and changes their admission in
one store. A project's `NAME` in the registry is its `[serve] name`. `--store PATH` names the database;
without it `project add` uses the added repository's store and the other
verbs the store of the repository the command runs in. `NAME` is the
repository directory's basename; a name that matches no row, or more than
one, is refused. See [Operating](../operating.md#registering-and-disabling-projects).

| Invocation | Does | Touches |
| --- | --- | --- |
| `project add PATH [--store PATH]` | validates `PATH` as a repository root whose `config.toml` passes and has a `[board]` table naming a team, then registers it enabled, recorded as `register_project`, without starting a run, and adds its resolved path to `host.toml`; a row the loop already wrote for the same team at the same path is adopted, and a row is recorded as `register_project` once however often it is adopted; refuses a path or name already in `host.toml`, naming the entry, and the same team at another path, naming the row. `host.toml` is rewritten whole through `host.toml.tmp`, created exclusively, and a rename, so two adds at once keep both. With `--store` naming a database other than the repository's own store, it registers in that store only and leaves `host.toml` untouched, since the registry holds paths and the host reads each project's own store | store, `host.toml` |
| `project remove NAME` | drops `NAME`'s entry from `host.toml`; touches no store; refuses a name the registry does not hold, listing the ones it does | `host.toml` |
| `project list [--store PATH]` | without `--store`, prints each project in `host.toml`: name, path, and the admission and note its own store holds (`-` without a store or row); a project whose config or store cannot be read is listed with `error=` and the rest still are, and the exit is then 1; with `--store`, each row of that store: name, path, admission, note and newest run | `host.toml`, store, read-only |
| `project enable NAME [--note TEXT] [--store PATH]` | enables admission again, recorded as `release_hold`; the note defaults to `enabled by operator`; refuses a project already enabled | store |
| `project hold NAME --note TEXT [--store PATH]` | stops new admission while existing runs finish, recorded as `hold`; refuses a project already held | store |
| `project disable NAME --note TEXT [--store PATH]` | stops admission, recorded as `disable`; a disabled project's supervisor exits at startup and `/status` reports the state and note with no runs; refuses a project already disabled | store |

## Host forms

A mode given no project means the host: every project listed in the host
registry, `HOLOPHYTE_HOME/host.toml`, which `project add` and `project
remove` write. `--status` is the one host form so far; any other mode
without a project is a usage error.

| Invocation | Does | Touches |
| --- | --- | --- |
| `--status [--json]` | the host form: the checkout's build and the last sweep's, the home's `supervisor.lock`, `sweep.json` (`sweep: none` without one), then every project in `HOLOPHYTE_HOME/host.toml` as the project form prints it, each line prefixed `[NAME]`; a project whose config, store or file cannot be read is its own error line and the exit is 1; no `host.toml` is exit 1 naming `project add` | `host.toml`, each store, read-only |

## Startup checks

Every mode validates every `config.toml` table it can see and refuses an
unknown key. The loop, `--worker`, `--supervise`, `--requeue`, `--approve`,
`--babysit`, `--close`, `--abort` and `--file-ticket` need a `[board]`
table; `--pause`, `--resume`, `--hold`, `--release-hold` and `--repoint` do
not. The loop additionally live-probes each configured agent
route and the reviewer image before claiming, and runs a read-only sweep
whose output it prints.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | done, or the board was empty |
| 1 | a startup refusal, a failed run under `stop_on_failure`, an invalid ticket file, a refused requeue, approval, babysitter, re-point, pause, resume, abort, close-out, hold or hold release, a refused `project` command |
| 2 | `--file-ticket`: the issue exists but its stored body failed re-validation; argparse errors, `project` commands' included |

## Output prefix

Every line the factory prints begins `[holo2]`; the verify gate's report
lines begin `[verify]`. A worker of the pool prints `[holo2 wN]` instead,
`N` its slot number, so the lines of a pool sharing one log tell apart. The loop's tmux log is the operator's first source
after the store.

## Environment

| Variable | Read by | Purpose |
| --- | --- | --- |
| `HOLOPHYTE_HOME` | `Project` | the state root, default `~/.holophyte`; tests point it at a temp dir |
| `LINEAR_API_KEY` | `linear_provider` | the board's API key; env or `.env` beside the module |
| `HOLOPHYTE_TARGET`, `HOLOPHYTE_SERVE_ADDRESS`, `HOLOPHYTE_SERVE_PORT` | the serve unit | one daemon instance's project, bind address, port |
