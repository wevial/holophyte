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
| `--board-diff PROJECT` | prints one line per board-owned field (title, body, priority, labels, column) where the store's row differs from the board's ready listing, one per listed issue with no store row, one per `ready` row with no live run the listing no longer names (in `[board] mode = "store"` only while its column is `ready`, since a store-mode ticket leaves the queue by its column), then a summary; exits 0 with none, 1 otherwise; a project with no `[board]` table exits naming the key | Linear (read), store, read-only |
| `--status [--json] PROJECT` | prints what the factory is doing now: projects, live and parked runs, ready tickets, schema, lock holders; `--json` prints it as one JSON object | store, read-only |
| `--import-store PATH --dry-run PROJECT` | opens the store at `PATH` and the project's own read-only and prints, per table, the rows an import would move, their id range, the offset a remap would add and a sha256 of the rows; refuses stores at different schema versions; `--dry-run` is required | store, read-only |
| `--board-import [--dry-run] PROJECT` | copies every open issue of the project's Linear board, Backlog included, into its store by board id in one transaction, before the project moves to the native board: a row the store holds keeps its id, runs, ledger and `dependsOn`, a new one takes the issue's column and open blockers; prints `[holo2] KO-n: new`, `changed` (its revision moved) or `unchanged` per issue, then `[holo2] board import: N new, M changed, K unchanged; P pushes and Q notes pending for Linear`; a failure part-way rolls back and running it again is the restart; `--dry-run` prints the same and writes nothing; `[board] kind = "native"` is refused with exit 1 before Linear is asked | Linear (read), store |
| `--supervise PROJECT` | the acting sweep every `sweep_interval_sec`, under the project's supervisor lock; runs the code it started with, and exits for its service manager to restart when a newer build has stamped the store; refused for a project `host.toml` lists, which the host sweep watches | store |
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
one store. `--store PATH` names the database; without it `project add` uses
the added repository's store and the other verbs the store of the
repository the command runs in. `NAME` means two things, by verb:

- `project remove NAME` matches a registry entry: its `[serve] name`, or
  the path it is registered under. It reads each entry's config on its
  own, so an entry whose config no longer loads, or two entries sharing a
  name, can still be removed by path.
- `project enable`, `hold` and `disable NAME` match a store row: the
  basename of the row's repository path. A name that matches no row, or
  more than one, is refused.

See [Operating](../operating.md#registering-and-disabling-projects).

| Invocation | Does | Touches |
| --- | --- | --- |
| `project add PATH [--store PATH]` | validates `PATH` as a repository root whose `config.toml` passes and has a `[board]` table naming a team, then registers it enabled, recorded as `register_project`, without starting a run, and adds its resolved path to `host.toml`; a row the loop already wrote for the same team at the same path is adopted, and a row is recorded as `register_project` once however often it is adopted; refuses a name already in `host.toml`, or a path already there whose store holds its row, naming the entry, and the same team at another path, naming the row. On a registered path whose store has no row for it (the store deleted or recreated after registration) it is the repair the daemon and the sweep name: it writes the row as above, says so, and leaves `host.toml` unchanged. `host.toml` is rewritten whole through `host.toml.tmp`, created exclusively, and a rename, so two adds at once keep both. With `--store` naming a database other than the repository's own store, it registers in that store only and leaves `host.toml` untouched, since the registry holds paths and the host reads each project's own store | store, `host.toml` |
| `project remove NAME\|PATH` | drops the entry whose `[serve] name` or registered path is given from `host.toml`; touches no store; refuses one the registry does not hold, listing the ones it does, and a name two entries share, naming both paths so one can be removed by path | `host.toml` |
| `project list [--store PATH]` | without `--store`, prints each project in `host.toml`: name, path, and the admission and note its own store holds (`-` without a store or row); a project whose config or store cannot be read is listed with `error=` and the rest still are, and the exit is then 1; with `--store`, each row of that store: name, path, admission, note and newest run | `host.toml`, store, read-only |
| `project enable NAME [--note TEXT] [--store PATH]` | enables admission again, recorded as `release_hold`; the note defaults to `enabled by operator`; refuses a project already enabled | store |
| `project hold NAME --note TEXT [--store PATH]` | stops new admission while existing runs finish, recorded as `hold`; refuses a project already held | store |
| `project disable NAME --note TEXT [--store PATH]` | stops admission, recorded as `disable`; a disabled project's supervisor exits at startup and `/status` reports the state and note with no runs; refuses a project already disabled | store |

## Host forms

A mode given no project means the host: every project listed in the host
registry, `HOLOPHYTE_HOME/host.toml`, which `project add` and `project
remove` write. `--status`, `--serve` and `--supervise` are the host forms;
any other mode without a project is a usage error.

| Invocation | Does | Touches |
| --- | --- | --- |
| `--status [--json]` | the host form: the checkout's build and the last sweep's, the home's `supervisor.lock`, `sweep.json` (`sweep: none` without one), then every project in `HOLOPHYTE_HOME/host.toml` as the project form prints it, each line prefixed `[NAME]`; a project whose config, store or file cannot be read is its own error line and the exit is 1; no `host.toml` is exit 1 naming `project add` | `host.toml`, each store, read-only |
| `--serve [HOST:PORT]` | the host daemon: every registered project's routes under `/projects/NAME/...` (`NAME` its `[serve] name`), and the host's `/status` and `/attention` at the root; serves on the socket the service manager hands over (`LISTEN_FDS`), else the address given, else `host.toml`'s `[serve] bind`; `host.toml`'s `[serve] machine_token_file` is the bearer beyond loopback and for every write, `[serve] actions` opens the project actions and `POST /actions/run-sweep`. On a factory `HEAD` move it exits 0 when handed its socket, and re-executes otherwise | `host.toml`, each store; writes only through the opt-ins, and `run-sweep` appends to `HOLOPHYTE_HOME/host-actions.jsonl` |
| `--supervise [--once]` | the host sweep: under `HOLOPHYTE_HOME/supervisor.lock` (a second run beside a live one exits 1 naming its pid), sweeps every registered store and bumps its one host-sweep beat (pid 0), then acts on the trips and reconciles pull requests, board closes and owed loops round-robin under a deadline of half `[supervisor] sweep_sec`; a project whose own `supervisor.lock` names a live pid is skipped, a dead one is removed; a disabled project, or one with no store or no row for its path, is skipped; writes `HOLOPHYTE_HOME/sweep.json` after every project; `--once` is one run, exit 1 when any project errored, and without it a run every `sweep_sec` until SIGINT/SIGTERM | `host.toml`, each store, `sweep.json`, Linear, GitHub, the loop units |

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
| `HOLOPHYTE_TARGET`, `HOLOPHYTE_SERVE_ADDRESS`, `HOLOPHYTE_SERVE_PORT` | the project units (`holophyte-serve@`, `holophyte-supervise@`), and `HOLOPHYTE_TARGET` alone the loop unit | one instance's project, bind address, port |
| `LISTEN_FDS`, `LISTEN_PID` | `--serve` | set by the service manager's socket unit: with `LISTEN_FDS=1` and `LISTEN_PID` this process's pid, the daemon serves on fd 3 instead of binding, and exits 0 on a factory `HEAD` move for the socket to start the new code |
