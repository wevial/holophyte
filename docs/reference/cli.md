# CLI

`python3 factory.py [MODE] PROJECT`. The command line is parsed, not
indexed, so `--help` is safe. Modes are mutually exclusive; the project is
always the repository path, except in the [host forms](#host-forms). `python3 factory.py project VERB` is a separate
command family, [below](#project-commands).

| Invocation | Does | Touches |
| --- | --- | --- |
| `factory.py PROJECT` | runs the loop: claim, work, verify, review, merge, repeat; exits on an empty board or a failed run; re-execs after a self-merge. Under `[loop] workers > 1` it is the scheduler of a pool of `--worker` children instead (see [The loop](../loop.md#the-pool)) | Linear, store, worktrees, `main`, `FINDINGS.md` |
| `--report PROJECT` | prints the estimate-vs-actual table and the supervisor liveness line, then a `failures KIND: N` line per failure kind among the failed runs (`unclassified` for one with no kind), a `gap layers:` line counting the gaps whose latest recorded layer is each of the six, in ladder order, and a `gaps found: witness N, operator M` line counting them by who found them; when the store holds a story, a `Stories:` section follows with each story's time from plan to approval and from approval to close (whole hours below two days, days and hours above, `-` for a step not taken), its children merged and abandoned, the interventions on its children's runs per child merge (one decimal, `not applicable` with none merged) and each witness's first green commit | store, read-only |
| `--sweep PROJECT` | prints what an acting sweep would do; lists stray review containers; a `failures KIND: N` line per failure kind among the failed runs | store (sightings only) |
| `--sweep --act PROJECT` | fails tripped runs, releases their leases, removes stray containers; prints the same `failures KIND: N` lines | store, Docker |
| `--board-diff PROJECT` | prints one line per board-owned field (title, body, priority, labels, column) where the store's row differs from the board's ready listing, one per listed issue with no store row, one per `ready` row with no live run the listing no longer names (in `[board] mode = "store"` only while its column is `ready`, since a store-mode ticket leaves the queue by its column), then a summary; exits 0 with none, 1 otherwise; a project with no `[board]` table exits naming the key | Linear (read), store, read-only |
| `--status [--json] PROJECT` | prints what the factory is doing now: projects, live and parked runs, two lines for each open story (state, generation, children merged/running/frontier/waiting, each witness's latest verdict at the last ledgered commit, errors, open decisions, age), ready tickets, schema, lock holders; `--json` prints it as one JSON object | store, read-only |
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
| `--gap-layer KEY-n LAYER --note TEXT [--carried-by KEY-n] [--found-by witness\|operator] PROJECT` | records the correction layer the lesson of the gap ticket `KEY-n` answers landed in: appends one `gapLayers` row with `LAYER` (`impossible`, `static`, `witness`, `guidance`, `review` or `none`), the note, the operator's user as author, `--carried-by`, the ticket carrying the lesson when it is not the gap's own, and `--found-by`, who found the gap (`foundBy`, `operator` when omitted); `--report`'s `gap layers:` line counts each ticket's latest row and its `gaps found: witness N, operator M` line counts those rows by finder. No `interventions` row is written: the verb changes no run, ticket or project state, and an intervention would count as human toil in `--report` and `/status`. An unknown layer or finder, or `--found-by` without `--gap-layer`, is a usage error (exit 2); a ticket the store does not hold exits 1 naming it | store |
| `--file-ticket TICKET.md [--state Todo\|Backlog] [--priority urgent\|high\|medium\|low] PROJECT` | validates, creates the issue in the Linear project the project's `[board]` names, reads it back, validates again | Linear |
| `--worker PROJECT` | internal, spawned by the scheduler under `[loop] workers > 1`: claims one ticket, works it to merge or park, exits with the run's status (0 merged, 1 failed, 2 parked, 3 nothing to claim, 4 stopped for a human); skips the startup probes, the sweep and the supervisor spawn, which the scheduler ran for the pool | Linear, store, worktrees, `main`, `FINDINGS.md` |
| `--file-ticket TICKET.md --update KO-n PROJECT` | same, replacing an existing issue's title, body and estimate; a blocker the file's `Depends on:` names and the board lacks is recorded and printed as `+KO-a`, one the board holds and the file no longer names is printed as `board also holds KO-c` -- relations are added, never removed | Linear |
| `--file-ticket TICKET.md --update KEY-n --revision N [--priority urgent\|high\|medium\|low] [--labels a,b] PROJECT` | on a native board: replaces the ticket's body, and its priority or labels when given, at revision `N`; a ticket at another revision exits 1 printing `KEY-n is at revision M, not N; nothing changed`, and every refusal of the store's is one line with exit 1; on a Linear board all three are refused beside `--update` | store |
| `--file-story SLUG [--priority urgent\|high\|medium\|low] PROJECT` | on a native board, or a Linear board in store mode: validates the story directory `stories/SLUG` in the project's state directory with the story validator, then files it in one transaction: the parent with the story body, its contract withheld, at `needs_spec` in Backlog; each child in Backlog in dependency order with the priority, its `Depends on:` sibling slugs rewritten to their identifiers; and the `stories`, `storyWitnesses` and `storyChildren` rows, the story `planned`. Prints `filed KEY-n: TITLE (ROLE, Backlog[, PRIORITY])` per ticket, then writes `Story: KEY-n` atop `story.md` and `Ticket: KEY-m` atop each child file. A story the validator refuses, a child depending on an unmerged ticket, or a `story.md` already headed `Story: KEY-n` (change a filed story with `--update`) exits 1 printing the problem, and nothing is written. On a Linear board every issue is created first, in Backlog: the parent unlabelled, each child a sub-issue of it with `[board] label` and a blocks relation per dependency; the store rows follow. A board failure part way exits 1 printing the identifiers already created, to cancel on Linear, and writes nothing to the store; a Linear board in mirror mode exits 1 | store, story directory |
| `--file-story SLUG --update KEY-n --revision N [--priority urgent\|high\|medium\|low] PROJECT` | on a native board: validates the story directory `stories/SLUG`, headed `Story: KEY-n`, and applies it to that filed story in one transaction with the parent read at revision N: the parent's body is replaced, its contract still withheld; each child file headed `Ticket: KEY-m` is edited at its current revision when its body changed; each child file without a header is filed in Backlog with the priority and gets its `Ticket:` header; and the story's witnesses, roles and standing orders are rewritten. A change to the parent body, a witness, a role, a dependency or the standing orders is a plan change: it records the parent's next revision, one per update, and returns an `approved` or `parked` story to `planned`, so the plan is approved again; a change to a child's other sections alone leaves the story as it is. Prints `updated KEY-m (revision R)` or `filed KEY-m: TITLE (ROLE, Backlog)` per changed child, then `story KEY-n is STATE at revision N`, the parent's committed revision. A parent not at revision N, a stored child whose file is gone (cancel a child with `--cancel`), a child file naming another story's ticket, or an invalid directory exits 1 printing the problem, and nothing is written; a Linear board exits 1 | store, story directory |
| `--approve-story KEY-n --revision N --note TEXT [--baseline-green W] [--baseline-red-kind exception W] PROJECT` | on a native board, or a Linear board in store mode: approves the `planned` story KEY-n with its parent read at revision N. A parent at another revision, a story not `planned`, another story of the project `approved` or `parked`, or an override naming a witness the story lacks exits 1 printing the problem before anything runs or is written. Then the baseline: every witness runs at main's tip with the story's witness files copied in, each verdict appended to the ledger as `baseline`, and `baseline at SHA: W1 red (assert), ...` is printed. A witness green at the tip exits 1 unless `--baseline-green W` names it, a witness red by exception exits 1 unless `--baseline-red-kind exception W` names it, and an `error` or `absent` verdict always exits 1; either override may be given more than once, and a kind other than `exception` is a usage error (exit 2). A refused approval keeps its baseline rows and leaves the story `planned` and its children where they are. On a pass one transaction freezes the plan with an `approve_story` intervention carrying the note, with ` (baseline overrides: W green, W red by exception)` when one applied, and releases every open child: on a native board moved to Ready at its current revision, on Linear given a queued Todo push the host sweep sends, its column following the board. Prints `approved story KEY-n at revision N` and `released KEY-m to ...` per child; a Linear board in mirror mode exits 1 | store, git |
| `--witness-pass KEY-n PROJECT` | runs a witness pass by hand on the `approved` or `parked` story KEY-n: every witness runs at main's tip as it stands on main, its file not copied in, so a witness whose file has not landed is `absent`; each verdict is appended to the ledger with verifier `operator`, even at a tip the ledger already holds, and `witness pass at SHA: W1 green, W2 absent, ...` is printed. A witness red at the tip whose ledger holds a green at an earlier commit is rerun once at the same commit and both rows are kept, the later one its verdict there. A witness whose verdict differs from the one at the previous commit in the ledger gets one note on the parent naming both, posted once per witness and commit. A ticket that is not a story's parent, a story not `approved` or `parked`, or a held project exits 1 printing why, and nothing runs | store, git |
| `--decide KEY-n ID [OPTION] --note TEXT PROJECT` | answers decision ID of the parked story KEY-n with OPTION, the option's number counting from 1 as the decision lists them, or `default`; left out, the decision's default. One transaction records the answer and a `decide` intervention carrying the note, and applies the option: "abandon the story" abandons the story and its parent and moves its unclaimed children to Backlog; "accept the changed witness file" makes the witness file at main's tip the witness's approved source and hash, so the next pass can close on it; "re-approve the plan as it stands" makes the child's current dependencies its approved edges; "amend the witness (re-plan)" and "drop W (re-plan)" return the story to `planned` for `--file-story --update` and a new `--approve-story`. "rerun" then runs a witness pass as verifier `operator`, which settles the story. "file a follow-up child", "file a fix child" and "restore the approved edges" ask a person to act first and are recorded only. With no decision left open, a parked story returns to `approved`. Prints `decision ID of story KEY-n: ANSWER` and `story KEY-n is STATE`. A ticket that is not a story's parent, a decision already answered, an ID the story does not hold, or an option number out of range exits 1 printing the problem, and nothing is written | store, git |
| `--move KEY-n ready\|backlog --revision N [--note TEXT] PROJECT` | on a native board: moves the ticket to Ready or Backlog at revision `N`, recording the note, and prints `moved KEY-n to COLUMN (revision M)`; a live run continues and its ticket is not claimed again; a ticket at another revision exits 1 printing `KEY-n is at revision M, not N; nothing changed`, and a refused move (a draft to Ready, a canceled or closed ticket) exits 1 printing the problem; on a Linear board a usage error (exit 2) that asks Linear nothing | store |
| `--cancel KEY-n --revision N --note TEXT PROJECT` | on a native board: cancels the ticket at revision `N`, recording the note, and prints `canceled KEY-n (revision M)`; a live run gets an `abort` intervention carrying the note and ends `abandoned` at its next safe point, and the line adds `; run R ends abandoned at its next safe point`; a last run parked awaiting merge approval gets a `close_out` intervention and ends `abandoned` at once, its pull request left open, and the line adds `; run R ended abandoned` and `, URL left open`; a last run parked `blocked_on_operator` after its merge landed cannot end abandoned, so the cancel exits 1 naming it and nothing changes; a stale revision or a refusal exits 1 as `--move`'s does; on a Linear board a usage error (exit 2) | store |

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

## holo

`holo COMMAND [ARGS] [-p NAME|PATH] [--verbose]` is the short form of the
modes above. Each command but `send-back`, `start` and `stop` stands for one
`factory.py` invocation and runs through the same parser, so its refusals, messages and
exit codes are the factory's own. A command's project is the first of these
that answers:

1. `-p NAME|PATH` on the command line;
2. `HOLO_PROJECT` in the environment, read as `-p` is;
3. the current repository: the top level of the git work tree the shell is
   in (`git rev-parse --show-toplevel`, so a subdirectory counts), when
   `host.toml` registers that path; any other directory does not answer;
4. `default_project` in `HOLOPHYTE_HOME/client.toml`, read as `-p` is.

A value holding a path separator, or naming an existing directory, is a
repository path; any other value is a `[serve] name` in `host.toml`, and a
name it does not register exits 2 naming the registered names rather than
asking the next source. With no source answering, `status`, `serve` and
`supervise` are the [host forms](#host-forms), as is the `attention` read
below, and any other command exits 2
naming all four sources. `--verbose` prints the project and the source that
named it to stderr. `NOTE` is free text, the last argument;
`-n/--note NOTE` is the same. `holo project VERB` is `factory.py project
VERB` with its arguments unchanged. `--worker` has no command: it is
internal, spawned by the loop's pool.

| Command | Aliases | Factory invocation |
| --- | --- | --- |
| `holo status [--json]` | | `--status [--json] [PROJECT]` |
| `holo report [--since WINDOW] [--notes] [--json]` | | none: the window's counts, read from the store as below; `factory.py --report PROJECT` is unchanged |
| `holo sweep [--act]` | | `--sweep [--act] PROJECT` |
| `holo board diff` | | `--board-diff PROJECT` |
| `holo board import [--dry-run]` | | `--board-import [--dry-run] PROJECT` |
| `holo store import PATH --dry-run` | | `--import-store PATH --dry-run PROJECT` |
| `holo file FILE [--backlog] [--priority P]` | `holo ticket file` | `--file-ticket FILE [--state Backlog] [--priority P] PROJECT` |
| `holo file FILE --update KEY [--revision N] [--labels a,b]` | `holo ticket file` | `--file-ticket FILE --update KEY [--revision N] [--labels a,b] PROJECT` |
| `holo move KEY ready\|backlog --revision N [NOTE]` | `holo ticket move` | `--move KEY ready\|backlog --revision N [--note NOTE] PROJECT` |
| `holo cancel KEY --revision N NOTE` | `holo ticket cancel` | `--cancel KEY --revision N --note NOTE PROJECT` |
| `holo requeue KEY NOTE` | `holo ticket requeue` | `--requeue KEY --note NOTE PROJECT` |
| `holo approve KEY [NOTE]` | `holo ticket approve` | `--approve KEY [--note NOTE] PROJECT` |
| `holo babysit KEY [NOTE]` | `holo ticket babysit` | `--babysit KEY [--note NOTE] PROJECT` |
| `holo send-back RUN NOTE` | | none: the console's send-back of run `RUN`, its note by the caller's login |
| `holo repoint KEY SHA NOTE` | `holo ticket repoint` | `--repoint KEY SHA --note NOTE PROJECT` |
| `holo pause KEY NOTE` | `holo ticket pause` | `--pause KEY --note NOTE PROJECT` |
| `holo resume KEY NOTE` | `holo ticket resume` | `--resume KEY --note NOTE PROJECT` |
| `holo abort KEY NOTE [--close-pr]` | `holo ticket abort` | `--abort KEY --note NOTE [--close-pr] PROJECT` |
| `holo hold NOTE` | | `--hold --note NOTE PROJECT` |
| `holo release NOTE` | | `--release-hold --note NOTE PROJECT` |
| `holo start [NOTE]` | `holo loop start` | none: `systemctl --user start holophyte-loop@NAME`, recorded first as the console's launch-loop is |
| `holo start --foreground` | `holo loop start` | `PROJECT`: the loop in this terminal |
| `holo stop NOTE [--now]` | `holo loop stop` | `--hold --note NOTE PROJECT`; with `--now`, then `--abort KEY --note NOTE PROJECT` for each live run |
| `holo close KEY URL [NOTE]` | `holo ticket close` | `--close KEY --landed URL [--note NOTE] PROJECT` |
| `holo gap KEY LAYER NOTE [--carried-by KEY] [--found-by F]` | `holo ticket gap` | `--gap-layer KEY LAYER --note NOTE [--carried-by KEY] [--found-by F] PROJECT` |
| `holo story file SLUG [--update KEY --revision N] [--priority P]` | | `--file-story SLUG [--update KEY --revision N] [--priority P] PROJECT` |
| `holo story approve KEY --revision N NOTE [--baseline-green W] [--baseline-red-kind KIND W]` | | `--approve-story KEY --revision N --note NOTE [--baseline-green W] [--baseline-red-kind KIND W] PROJECT` |
| `holo story witness KEY` | | `--witness-pass KEY PROJECT` |
| `holo story decide KEY ID [OPTION] NOTE` | | `--decide KEY ID [OPTION] --note NOTE PROJECT` |
| `holo supervise [--once]` | | `--supervise [--once] [PROJECT]` |
| `holo serve [ADDR]` | | `--serve [ADDR] [PROJECT]` |

`holo start` starts the project's loop unit and returns: it records a
`launch_loop` intervention, then runs `systemctl --user start
holophyte-loop@NAME`, `NAME` the project's `[serve] name` in `host.toml`,
and prints the unit and the ready count. A project with no `[serve] name`
in `host.toml` has no unit and is refused naming the entry, as is a
disabled project. A held project needs
the note: `start` releases the hold with it before starting the unit, and
without one it exits 1 naming the hold. Closing the terminal leaves the
unit running. `holo start --foreground` is `factory.py PROJECT`, the loop
in this terminal.

`holo stop NOTE` holds the project's admission as `holo hold NOTE` does,
so the loop admits no new ticket and the host sweep starts no loop for it;
a project already held is refused as `hold` refuses it. Its live runs, every run
not ended and not parked, finish their work, and the loop exits at its next
idle check once none is left; `stop` names the runs it waits for. `holo
stop --now NOTE` also aborts each live run as `holo abort` does, with the
note: each stops at its next safe point and keeps its work. Neither stops
the unit, because stopping it kills a live run mid-turn and loses its work;
the loop exits on its own once the hold leaves it idle.

The write commands, each that takes a `NOTE` and `holo file`, print one
result. Without `--json` it is one line: `✓` and what the verb did, then
`intervention N recorded` when it wrote an interventions row, on stdout; or
`✗` and the refusal on stderr. With `--json` it is one JSON object on
stdout, the keys the daemon's `POST /actions/...` answers use:

| Key | Value |
| --- | --- |
| `action` | the command's words, `requeue` or `story approve` |
| `ok` | `true` when the verb exited 0 |
| `detail` | the verb's message or its refusal, without the `[holo2]` prefix |
| `recorded` | the id of the interventions row the verb wrote, or `null` when it wrote none (a refusal, `gap`, `move`, `file`) |
| `ticket` | the `KEY` the command names, or `file --update KEY`'s, when it names one |
| `run` | the run the interventions row is on, or `send-back`'s `RUN` |

```
$ holo requeue HOLO-133 "rerun" --json -p holophyte
{"action": "requeue", "ok": true, "detail": "HOLO-133 requeued after run 941", "recorded": 1704, "ticket": "HOLO-133", "run": 941}
```

`--json` changes the output, never the exit code below: a usage error is
still exit 2, its result `ok: false` with the error line as `detail`.

`holo status` without `--json` renders the `--status --json` object, the
project form or the host form, as a page read top down; with `--json` it
prints that object unchanged. Its lines carry no `[holo2]` prefix:

| Section | Lines |
| --- | --- |
| header | the project or the home, then the date and clock time, `Tue Oct 6, 10:42 PDT` |
| `Needs you (N)` | `!`, the project, the ticket and the question of each parked ticket; a project whose admission is held or disabled, with its note and ready count; a story planned and waiting on approval, or parked with its open decisions counted; the reason of each stranded run and how long ago it ended, `14 min` |
| `Running (N)` | `>`, the project, the ticket, the phase and the heartbeat's age, `heartbeat 20 s ago`; a merge lock or a project supervisor lock held by a live holder |
| `Quiet` | `✓` and the ready count of each enabled project with nothing live, parked, stranded or wrong and no story planned or parked |
| `Problems (N)` | `✗` and a `try:` hint for a lock naming a dead pid, a run no longer live or no holder, an unreadable project, a project the last sweep errored on, a sweep that failed, or one that started over two minutes ago and never ended; a project here has no quiet line |
| footer | the sweep's state and age, `Sweep ok 40 s ago` or `Sweep killed, started 94 h ago`, the home lock's live pid, and the build as a short hash, `build 92ef2b0` (host form only) |

A free lock prints no line. Ages are `s` under a minute, `min` under an
hour, then `h`. The symbols are coloured (`!` orange, `>` blue, `✓` green,
`✗` red) only when the output is a terminal and `NO_COLOR` is unset or
empty; the write commands' `✓`/`✗` follow the same rule.

`holo report` reads the project's store read-only and opens with counts
for a window, `--since`: `Nh` or `Nd` (`24h`, `7d`, `30d`), or `all`;
default `7d`. Any other form exits 2 naming the accepted ones. A run is in
the window when it ended in it; a live run is in no count. A project with
no store exits 1. Its lines carry no `[holo2]` prefix:

| Section | Lines |
| --- | --- |
| header | the project's directory and the window, `holophyte · last 7 days` |
| `Shipped` | the merged, abandoned and failed runs, and the median minutes of work per merged run with the median estimate beside it, `3 merged · 1 abandoned · 2 failed · median 16 min per ticket (estimate 30)`; with none of the three, `nothing shipped in the last 7 days` |
| `Failures` | the failed runs by failure kind, most first, `verify 3 · infra 1`, or `none` |
| `Gaps` | the gaps whose latest layer is `none`, as open, then each other layer holding a gap, over the whole store as `factory.py --report`'s `gap layers:` line counts them |
| `Hands-on` | the human interventions by action, `3 interventions (requeue 2 · approve 1)`, and the send-backs, the `operator_note` events |
| `Runs (N)` | the window's ended runs in end order: `✓` merged, `✗` failed, `·` otherwise, the ticket, the outcome, the work against the estimate in minutes, the review rounds and how long ago it ended |
| `Notes (N)` | with `--notes` only: each send-back note consumed in the window, newest first, with the time it was consumed, the ticket, run and round, and its author |

`holo report --json` prints the object the page renders from, the notes
included with or without `--notes`:

| Key | Value |
| --- | --- |
| `project` | the repository path |
| `window` | `since` as given, `from_ms` the window's start (`null` for `all`) and `now_ms` |
| `shipped` | `merged`, `abandoned`, `failed`, `median_min` and `median_estimate_min` (`null` with no merged run measured) |
| `failures` | failure kind to count |
| `gaps` | `open`, `layers` (each layer to its count) and `found_by` (each finder to its count) |
| `hands_on` | `interventions`, `by_action` (action to count) and `send_backs` |
| `runs` | the window's runs in end order, each with `run`, `ticket`, `actual_min`, `agent_min`, `verify_min`, `estimate_min`, `ratio`, `rounds`, `outcome`, `host`, `ended_ms`, `merge_sha` and `wall_min`, as a `GET /runs` row has them |
| `notes` | the window's consumed notes, newest first: `run`, `ticket`, `round`, `event_id`, `author`, `note` and `consumed_ms` |

Five reads are no `factory.py` mode: each calls, in process, the view
function the [serve daemon](http.md) answers its route with, so the command
and the route agree. Each opens the store read-only and writes nothing.
`--json` prints the route's body as the daemon would send it; without it,
each prints a compact listing, one line per run, item, column entry, file,
ledger entry or turn. The view's status is the exit: 200 is 0, 400 is 2,
and 404, 503 or any other is 1, with the body's `error` on stderr. The
project is found as above; `attention` with none is the host form, the host
daemon's root `/attention` built from `host.toml` with no daemon running.

| Command | Aliases | Route whose body `--json` prints |
| --- | --- | --- |
| `holo runs [--limit N] [--json]` | | `GET /runs[?limit=N]` |
| `holo run N [--json]` | `holo run show N` | `GET /runs/N` |
| `holo run N --files\|--ledger\|--turns [--json]` | `holo run show N` | `GET /runs/N/files`, `/runs/N/ledger`, `/runs/N/turns` |
| `holo attention [--json]` | | `GET /attention`, or the host daemon's root `GET /attention` |
| `holo board [--json]` | | `GET /board` |
| `holo ticket KEY [--json]` | | `GET /tickets/KEY` |

`HOLOPHYTE_HOME/client.toml` is the client config every `holo` command reads
first; a key it does not hold below, or a file TOML cannot read, exits 2
naming the file.

| Key | Purpose |
| --- | --- |
| `default_project` | the project, a `[serve] name` or a repository path, when `-p`, `HOLO_PROJECT` and the current repository give none |
| `timezone` | the zone `holo` pages show clock times in, an IANA name such as `"America/Los_Angeles"`; default the local zone; a name `zoneinfo` does not know exits 2 naming the key and the value |

## Startup checks

Every mode validates every `config.toml` table it can see and refuses an
unknown key. The loop, `--worker`, `--supervise`, `--requeue`, `--approve`,
`--babysit`, `--close`, `--abort` and `--file-ticket` need a `[board]`
table; `--pause`, `--resume`, `--hold`, `--release-hold`, `--repoint` and
`--gap-layer` do not; `--move` and `--cancel` need a native one. The loop additionally live-probes each configured agent
route and the reviewer image before claiming, and runs a read-only sweep
whose output it prints.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | done, or the board was empty |
| 1 | a startup refusal, a failed run under `stop_on_failure`, an invalid ticket file, a refused requeue, approval, babysitter, re-point, pause, resume, abort, close-out, hold or hold release, a refused `project` command, a stale revision or refused move or cancel; a `holo` read answered 404, 503 or any status but 200 and 400; `holo report` on a project with no store |
| 2 | `--file-ticket`: the issue exists but its stored body failed re-validation; argparse errors, `project` commands' included; `holo`: no project found, a name `host.toml` does not register, or a refused `client.toml`; a `holo` read answered 400; a `holo report --since` window it does not take |

## Output prefix

Every line the factory prints begins `[holo2]`; the verify gate's report
lines begin `[verify]`. A worker of the pool prints `[holo2 wN]` instead,
`N` its slot number, so the lines of a pool sharing one log tell apart. The loop's tmux log is the operator's first source
after the store.

## Environment

| Variable | Read by | Purpose |
| --- | --- | --- |
| `HOLOPHYTE_HOME` | `Project` | the state root, default `~/.holophyte`; tests point it at a temp dir |
| `HOLO_PROJECT` | `holo` | the project, a `[serve] name` or a repository path, when no `-p` is given |
| `NO_COLOR` | `holo` | set and not empty, `holo` prints its symbols without colour on a terminal too |
| `LINEAR_API_KEY` | `linear_provider` | the board's API key; env or `.env` beside the module |
| `HOLOPHYTE_TARGET`, `HOLOPHYTE_SERVE_ADDRESS`, `HOLOPHYTE_SERVE_PORT` | the project units (`holophyte-serve@`, `holophyte-supervise@`), and `HOLOPHYTE_TARGET` alone the loop unit | one instance's project, bind address, port |
| `LISTEN_FDS`, `LISTEN_PID` | `--serve` | set by the service manager's socket unit: with `LISTEN_FDS=1` and `LISTEN_PID` this process's pid, the daemon serves on fd 3 instead of binding, and exits 0 on a factory `HEAD` move for the socket to start the new code |
