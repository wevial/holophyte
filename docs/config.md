# Config

Every `config.toml` table the factory reads, with a commented example of each.
A ticket that adds a config table edits this file; one that adds a mode edits
the README's usage block. Back to the [README](index.md).

## Config

`LINEAR_API_KEY` — an env var or `.env` next to `linear_provider.py`. Which
Linear project a target is driven from is the `[board]` table of that
target's `config.toml`, below.

Per-target behavior lives in `~/.holophyte/<slug>/config.toml`. Everything the
factory keeps about a target sits in that one directory — the store at
`store.db`, the supervisor lock — created on first need; only
`<repo>.worktrees` keeps a sibling address of its own.

The directory is host state, not repo state: it holds this host's agent
routes, leases and heartbeats, so it belongs to the host rather than to a
checkout that gets cloned, moved and deleted. `<slug>` is the target's
basename plus the first eight hex digits of the SHA-1 of its absolute path,
so `/a/repo` and `/b/repo` — two repositories with two histories — never
share a store. Set `HOLOPHYTE_HOME` to put the whole tree somewhere other
than `~/.holophyte`; the tests use it, and nothing else reads a home path.

Older layouts are adopted once, on the first retarget that finds them: a
`<repo>.holophyte/` directory beside the checkout, or the dotted siblings
that preceded it (`<repo>.holophyte.db` with its `-wal`/`-shm` sidecars and
`<repo>.holophyte.toml`), are moved into the state directory with one
`[holo2] adopted <from> -> <to>` line per file. What ends adoption is the
store at the new address, not the directory holding it, so writing
`config.toml` there by hand first does not strand a legacy history — the move
merges into the directory that is already there. If a store is already at the
new address and another is still at an old one, the factory exits non-zero
naming both and moves nothing: which history is the real one is an operator's
decision, not a guess. A single file already sitting at a landing address —
that hand-written `config.toml`, say, with a legacy `<repo>.holophyte.toml`
still beside the checkout — stops the move the same way rather than being
overwritten.

Adoption runs for the target the command line names, once `cli()` has named
it. Importing the module or asking for `--help` derives paths and moves
nothing.

The file is optional:
absent means every default below stays in place, which is how the factory runs
against itself. A file that exists but does not parse is a startup error naming
the file and the line — a config the operator wrote is never silently ignored.
Tables this version does not know are left alone. Inside a table it does read
(`[agents]`, `[worktree]`, `[supervisor]`, `[loop]`, `[report]`, `[board]`, `[merge]`, `[console]`, `[serve]`), a key it does
not read is
a startup
error naming the file, the table, the key and the keys the table accepts:
`setup_timeout_min` is a typo, not a timeout, and a typo the factory ignored
would leave a knob believed set that is not. The accepted keys are listed with
each table below.

```toml
[agents]
# Each role's harness command. The task goal is appended as the last argument,
# so end the command where its prompt goes (`-p` for Claude Code, nothing for
# `codex exec`). Omit a role to keep its default.
implementer = "claude --model opus --effort high -p"   # default route
reviewer    = "my-reviewer --diff"                     # see the caveat below
adjudicator = "my-reviewer --final"
# The Codex model and reasoning effort the review container runs, for the
# reviewer and the adjudicator alike. Optional; the values shown are the
# defaults. The effort is one of low, medium, high, xhigh.
review_model  = "gpt-5.6-sol"
review_effort = "medium"
```

Accepted keys: `implementer`, `reviewer`, `adjudicator`, `review_model`,
`review_effort`.

`review_model` and `review_effort` choose what runs inside the hardened
container when neither review role is overridden by a command. Both reach the
container script as arguments, never as text spelled into it. The profile a
review round records is `codex-TAIL-EFFORT`, where TAIL is the model id's last
dash-separated segment: the default records `codex-sol-medium`, and
`gpt-6-astra` at medium records `codex-astra-medium`, so the row names what
actually ran. An effort outside the four above or an empty model is a startup
error naming the key. Either key beside a `reviewer` command is refused as
contradictory: the override opts the reviewer out of the container the pair
routes, so one of the two lines is not doing what it says.

Defaults, in place whenever the key is absent: `claude -p <goal> --model opus
--effort high` implements; `review` and `adjudicate` go through the hardened
container described in [Reviewing](reviewing.md). **A `reviewer` or `adjudicator` override is also an
opt-out of that container** — the configured command runs directly in the task
worktree. Overriding the implementer has no such effect; it already runs there.

What an override keeps is the pair the round is about. Before the command runs,
the task worktree's `refs/review/base` and `refs/review/candidate` are pointed
at the round's two commits — the same names the staged checkout uses, and the
names the reviewer prompt tells the command to read. Both must be full commit
SHAs the worktree has, with the base an ancestor of the candidate, or the round
is refused rather than run against whatever `HEAD` happens to be.

Every configured command is resolved at startup, before the run claims a
ticket: the string has to split to an argv, and its program has to be an
executable found on `PATH` or named by an absolute path. A name that resolves
nowhere is an error while nothing is in flight, rather than a
`FileNotFoundError` in the middle of a round holding the project's run lease.
Startup does not *run* the command — a route is an agent turn, not a probe.
Relative paths with a directory in them (`./review.sh`) are refused: rounds run
in a task worktree that does not exist yet, so the name would resolve somewhere
neither startup nor the operator named.

```toml
[worktree]
# Shell commands that prepare a freshly cut task worktree, run in order.
setup = [
  "python3 -m venv .venv",
  ".venv/bin/pip install -q -e '.[dev]'",
]
# Wall-clock cap per setup command, in seconds. Optional; the default is the
# verify gate's 300-second cap.
setup_timeout_sec = 300
# The segment ahead of the slash in a task branch name. Optional; `task` when
# absent, so branches are `task/ko-7000-the-title-slug`.
branch_prefix = "task"
# Ignored install directories the review stage copies from the task worktree,
# read-only, so the reviewer can run the ticket's verify commands. Optional;
# empty when absent.
carry = ["console/node_modules"]
```

Accepted keys: `setup`, `setup_timeout_sec`, `branch_prefix`, `carry`.

They run in the worktree, right after its branch is cut and before the first
agent turn — the moment that decides what the implementer and the verify gate
have to work with. Without them a worktree silently borrows the main checkout's
environment (its `.venv`, its module cache), so a task that changes a dependency
is tested against the old one. Each command goes through the same machinery as a
ticket's verify command: shell, one command per entry, a per-command cap
(`setup_timeout_sec`, a positive number of seconds; 300 when absent), and a
fail-loud report that names the failing command, the cap when it is the cap
that fired, and its output, attributing a top-level `&&` chain clause by
clause.

A failing command stops the setup — step two of a setup assumes step one worked
— and fails the run before an agent turn is dispatched, so a target whose
toolchain will not install costs no tokens. The branch and worktree are
discarded rather than preserved: no agent ran, so there is nothing on them to
keep, and the reason goes to the ticket as a comment. The table's shape is
checked at startup with the `[agents]` commands; the commands themselves are not
run there, since the worktree they are written against does not exist yet.

What setup writes into the worktree is untracked, and the implementer is asked
to commit its work: keep build artifacts (`.venv/`, caches) in the target's
`.gitignore`, or a task's `git add -A` will sweep them into the branch.

`carry` lists the repository-relative directories, among what setup wrote and
git ignores, that the review stage receives a copy of: the reviewer judges a
fresh checkout of the candidate commit, which holds none of them, and a
console ticket's `bun --cwd=console test` reports zero tests in a stage with no
`console/node_modules`. Each listed directory is copied into the stage at the
same path with its write bits cleared, after the checkout and before the
reviewer starts; the stage's identity check runs before and after the copy
with `--ignored=no`, so a carried directory neither dirties the stage nor
counts in its fingerprint. A listed path that is tracked in git, absent from
the worktree, or escapes the repository (`..`) fails the stage naming the path
rather than skipping it. Startup checks the list is a list of relative paths;
nothing is carried that the worktree does not already hold.

`branch_prefix` names the segment before the slash in every branch the loop
cuts, so a repository with its own convention (`factory/`, `ko/`, `bot/`) keeps
it. Everything after the slash is unchanged — the lowercased ticket identifier,
then the title slug — because the identifier is what makes a preserved branch
traceable from `git branch` alone. The worktree directory name does not carry
the prefix and does not change. A prefix that is empty, contains a slash or
whitespace, starts with `-`, or uses a character git refuses in a ref name
(`~ ^ : ? * [ \`) is a startup error naming the key, before anything is claimed. Branches already
preserved under an older prefix are not renamed; a run that reuses one starts
from the name the new prefix gives it.

```toml
[supervisor]
# The sweep's thresholds. Every key is optional; the values shown are the
# defaults, in place whenever the key (or the whole table) is absent.
heartbeat_stale_min      = 5    # a heartbeat older than this is a silent sighting
stale_strikes            = 2    # consecutive silent sightings that trip a run
budget_grace             = 1.5  # multiple of the ticket's estimate that blows the box
review_overlap_threshold = 0.5  # findings shared by two rounds that reads as stuck
sweep_interval_sec       = 60   # sleep between two --supervise passes
restart_grace_sec        = 120  # how long a self-merge re-exec may take to come back
```

Accepted keys: the six above.

The box is counted per turn: a run's allowance is the ticket's estimate once
for its first implementer turn and once more for each review round it has
recorded, up to the run's review cap, all under `budget_grace` -- the same
budget the loop gives each turn, so a fix round after a review is not swept as
overtime. A run with no review round yet is judged against the single box.

Different targets want different patience — a Go build's worktree setup is
slower than stdlib Python's — and these are the knobs `--sweep` and
`--supervise` read. Each value is checked at startup, for every mode: the
thresholds and the interval must be positive numbers, `stale_strikes` a
positive integer, and the overlap a fraction in (0, 1]. A value outside its
constraint is an error naming the key and the constraint, like malformed TOML,
rather than a default quietly used in its place. A key this version does not
know is refused the same way. The config is read once at startup; a running
supervisor does not pick up an edit.

```toml
[loop]
# What the claim loop does after a run it closed out as failed. Optional; the
# value shown is the default.
stop_on_failure = true   # false: record the failure and claim the next ticket
# Which ready ticket the loop claims first. Optional; the default is the
# lowest identifier.
order = "identifier"     # "priority": most urgent Linear priority first
# Whether the loop starts a detached --supervise for the target at startup
# when no live supervisor holds its lock. Optional; the default is true.
spawn_supervisor = true  # false: a service manager runs the supervisor
# The review-round cap, computed per run from the candidate's diff. Optional;
# the values shown are the defaults.
review_rounds = 2            # the base every run gets
review_rounds_per_lines = 800  # one extra round per this many changed lines; 0 never scales
review_rounds_max = 4        # the ceiling
# How many worker processes the loop keeps running at most, one per claimable
# ticket. Optional; the default is one process working one ticket at a time.
workers = 1                  # 3: up to three tickets worked at once
# How often, in seconds, the scheduler recounts the queue while fewer than
# `workers` are running. Optional; the default is two minutes.
tick_sec = 120
```

Accepted keys: `stop_on_failure`, `order`, `spawn_supervisor`,
`review_rounds`, `review_rounds_per_lines`, `review_rounds_max`, `workers`,
`tick_sec`.

By default one failed run ends the process after its close-out, with a nonzero
exit, and an operator relaunches the loop — the right call while the loop is
still being watched. With `stop_on_failure = false` the run is closed out
exactly as before (released, escalated if it was one failure too many, the
`FINDINGS.md` window regenerated) and the loop goes on to the next ready ticket
in the same process, for an unattended night. Escalation is untouched: a ticket
that fails twice still parks itself for a human; the knob only decides whether
one failure stops the whole queue. The exit status is still nonzero once the
queue is empty if any run failed. The value must be a boolean, `true` or
`false`; a string such as `"yes"` is a startup error naming the key, like a
`[supervisor]` threshold outside its constraint.

`order` is which ready ticket the loop claims first. `"identifier"` (the
default) is the loop as it has always been: lowest identifier first.
`"priority"` claims the most urgent Linear priority first (1 before 2 before
3 before 4, then unprioritised), identifier ascending within a priority --
the policy for a queue with more than one author, where a P1 filed after ten
P3s should not wait behind all of them. The file board has no priority and
orders by identifier under either value. Anything but one of the two strings
is a startup error naming the key.

With `spawn_supervisor = true` (the default) the loop checks the target's
`supervisor.lock` at startup, after the config and route checks and before
its first claim, and when no live pid holds it starts `factory.py --supervise`
for the same target as a detached process, logging to `supervisor.log` in the
state directory; when a live supervisor holds the lock it names that pid and
carries on. `spawn_supervisor = false` skips the check and the spawn, for an
operator whose service manager runs the supervisor as a unit of its own; the
explicit `--supervise` command is unchanged either way. A boolean, checked
like `stop_on_failure`.

The three `review_rounds*` keys set how many review rounds a run may take
before its terminal adjudication. Before the first review the loop measures
the candidate against its merge base with main (`git diff --numstat`,
insertions plus deletions, so a preserved branch that already merged main is
not charged for main's own lines) and computes

```
cap = min(review_rounds_max, review_rounds + changed_lines // review_rounds_per_lines)
```

It prints `[holo2] review cap N for M changed lines`, records the same line
as a note in the run's ledger, and runs up to `N` rounds; the adjudication
after the last round is unchanged. Under the defaults a 300-line candidate
gets two rounds, a 1,700-line one four, and a 9,000-line one four. With
`review_rounds_per_lines = 0` every run gets exactly `review_rounds`. Each key
must be an integer: `review_rounds` and `review_rounds_max` at least `1`,
`review_rounds_per_lines` at least `0`, and `review_rounds_max` no less than
`review_rounds`; anything else is a startup error naming the key.

`workers` is the ceiling on the pool of worker processes the loop keeps
running. With `1` (the default) the loop is one process working one ticket
at a time, exactly as before the key. Above `1` the process that ran
`factory.py TARGET` becomes a scheduler: it runs the startup checks and the
sweep once, then keeps `min(claimable, workers)` children running, each a
`factory.py TARGET --worker` that claims one ticket and works it to merge or
park -- a queue of one ticket is one worker, a queue of five under
`workers = 3` is three (see [The loop](loop.md#the-pool)). Merges into `main`
still serialise under the merge lock. `stop_on_failure` keeps its meaning
per pool: a failed worker stops the spawning and the running workers are
waited for. An integer of at least `1`; `"3"` (a string) or `0` (a pool
that could work nothing) is a startup error naming the key.

`tick_sec` is how often the scheduler recounts the queue while the pool is
below `workers`: with a slot free, its wait on the children times out after
this many seconds and the ready listing and the claimable count are run
again, so a ticket filed while the pool was busy starts within a tick rather
than at the next exit. With the pool full the scheduler waits on exits
alone, and the tick prints nothing unless it spawns. An integer of at least
`10`; `"120"` (a string) or `5` is a startup error naming the key. Only the
scheduler reads it: under `workers = 1` there is no pool to tick.

```toml
[board]
# The Linear project this target claims from and the team whose workflow
# states its tickets move through. Required for the loop and --supervise.
project_id = "00000000-0000-0000-0000-000000000000"
team = "Example Team"
```

Accepted keys: `project_id`, `team`.

The board is a per-target setting: two targets on one host driven from one
process-wide variable would both claim from the same project, and the second
would silently work the first's queue. Both values must be non-empty strings.
`--report`, `--serve`, `--repoint` and a read-only `--sweep` need no board and
run without the table; the loop and `--supervise` exit at startup naming `[board]
project_id` when it is absent. Nothing in the environment stands in for the
table.

```toml
[report]
# What the factory prints where it would print the machine's hostname.
# Optional; absent, the hostname is printed as recorded.
host_label = "writer-1"
# Whether the loop keeps FINDINGS.md: `window` renders and commits the
# bounded window at every close-out, `off` neither writes nor commits the
# file. Optional; the default is `window`.
findings = "window"
```

Accepted keys: `host_label`, `findings`.

`findings = "off"` is for a target that does not want the rendered file:
the run's ledger lives in the store either way and the daemon serves it
from `/runs/N/ledger`, so nothing is lost but the projection. A
`FINDINGS.md` already in the repository is left exactly as it is, not
deleted. `--report` prints the mode in effect below the table.

The `host` column of `--report` and `--sweep` and the supervisor's startup
and refusal lines show the label in place of the hostname when it is set.
The `FINDINGS.md` window the loop commits renders no host: its run and round
entries never carried one, so there is nothing there to relabel. The column
of the report and sweep exists so a reader
can tell which writer produced a run when there is more than one; a stable
label does that job without naming a personal machine in a public repository.
The store keeps recording the real hostname (`runs.host`,
`supervisorHeartbeats.host`, the lock file), which the supervisor compares
against its own, so the label can be renamed later without a migration. The
value must be a non-empty string; anything else is a startup error naming the
key.

```toml
[console]
# The other daemons the console fans out to, as HOST:PORT strings. Optional;
# absent, the page shows this daemon's project alone.
daemons = ["writer-2:7710", "writer-3:7710"]
```

Accepted keys: `daemons`.

One daemon serves one project; the console shows every project on every
host, so the page has to be told where the others are, and the daemon it was
loaded from tells it: `GET /peers` answers this list as `peers` beside the
address the daemon itself bound as `self`. The entries are strings the page
fetches from the browser; the daemon never connects to them. Each is checked
at startup like `--serve`'s address -- a non-empty host, a decimal port -- and
none may appear twice; a bad entry is a startup error naming `[console]
daemons` and the entry, before anything is served. A peer beyond loopback
is behind its own `[serve] token_file`; the page presents the token the
operator gives it.

```toml
[serve]
# The file whose contents every JSON request to a non-loopback bind must
# present as `Authorization: Bearer ...`. Required when `--serve` names a
# host other than loopback; ignored when it binds loopback.
token_file = "~/.holophyte/holophyte/serve.token"
# Answer `POST /actions/restart-supervisor`, `/actions/launch-loop` and
# `/actions/requeue` behind the token, on every bind (so `token_file` is
# required with this on). Off, every `/actions/` path is 404.
actions = false
# The systemd instance those actions address: `holophyte-supervise@NAME`,
# `holophyte-loop@NAME`. The target directory's name when absent.
name = "holophyte"
```

Accepted keys: `token_file`, `actions`, `name`.

The daemon's bind address is its only boundary, and once the bind is
anything but loopback that is not enough. With `--serve HOST:PORT` where
`HOST` is not loopback (`127.0.0.1`, any `127.x` address, `localhost`, `::1`),
the daemon reads `token_file` at startup and answers 401 with an empty JSON
body, before touching the store, to every request that does not carry the
file's exact contents as a bearer token; `/`, the console's built files and
`/peers` stay open so the page can load and learn where its peers are. A
non-loopback bind with no `token_file` is a startup error naming the key. The
file must be a regular, non-empty file that is not group- or world-readable
(`chmod 600`); anything else is a startup error naming the file and its
mode. The token is the file's contents with surrounding whitespace stripped
and is never printed or logged. `~` is expanded and a relative path is taken
against the config's directory. A loopback bind ignores the key for its
reads: `--serve 7710` is as open as it always was, unless `actions` is on
(below). One token per target, no rotation: to change it, write the file
and restart the unit.

`actions` opts the daemon into the three `POST /actions/...` routes, off by
default: `restart-supervisor` and `launch-loop` run `systemctl --user`
against the deploy units, `requeue` is the store's requeue as `--requeue
KO-n --note TEXT` does it, each an interventions row written before it
runs and not run when it cannot be recorded. The routes are behind the
bearer token on every bind, loopback included -- the bind address guards
reads, not a hand on the units -- so `actions = true` needs `token_file`
whatever the bind, and a bind without it is a startup error naming the
key. `name` is the
instance name the unit actions append -- the slug the deploy templates were
enabled under -- a non-empty string with no `/`, the target directory's
name when absent. Both are read once at bind. The routes, their bodies and
replies are in [The daemon's actions](reference/daemon.md).

```toml
[merge]
# Who says "merge" once the reviewer has approved and the pre-merge verify
# has passed. Optional; the value shown is the default.
approve = "auto"   # "human": park the approved run for an operator to release
# Where an approved, verified candidate goes. Optional; the value shown is
# the default.
mode = "local"     # "pr": push the branch to origin and open a pull request
# How many shepherd passes over an open pull request before the run parks
# for the operator. Optional; the value shown is the default.
pr_rounds = 5
# How the shepherd merges a green, quiet pull request: "merge", "squash" or
# "rebase". Optional; the value shown is the default.
pr_merge_method = "merge"
# Where the pull request's title and body come from: "ticket" (the ticket
# pasted, titled `KO-n: TITLE`) or "written" (one implementer turn writes
# them from the diff). Optional; the value shown is the default.
pr_text = "ticket"
# Instructions the written turn is given, in the repository's own words.
# Optional; default empty.
pr_style = ""
# Shell commands run in order in the main checkout once a local merge has
# landed, before the run is marked merged: the console build, so the daemon
# serves the bundle the merge changed. Optional; default empty. The first
# failing command stops the list and parks the run for the operator with
# its output; the merge stays. Not run under mode = "pr".
after = ["bun --cwd=console run build"]
```

Accepted keys: `approve`, `mode`, `pr_rounds`, `pr_merge_method`, `pr_text`,
`pr_style`, `after`.

With `approve = "auto"` a clean merge gate merges, as it always has. With
`approve = "human"` the loop stops there instead: the run's phase becomes
`awaiting_merge_approval`, its ticket goes `blocked_on_operator` with the
question `merge?` (which `/attention` lists under `blocked`, and the drawer
shows), a ledger comment names the branch and the candidate sha, and the
branch and worktree are preserved exactly as after a refused merge. The lease
is released, so the loop claims the next ready ticket, but the run itself is
not ended: it stays open in `awaiting_merge_approval` with no outcome, the
supervisor sweep leaves it alone as it does a run blocked on an operator, and
the park is not a failure -- it neither stops the pass under
`stop_on_failure` nor counts toward the escalation that blocks a ticket, and
the exit status is not spent on it. Nothing merges until the operator says
so; releasing a parked run is the next ticket's `--approve`. The value must
be `"auto"` or `"human"`; anything else is a startup error naming the key.

With `mode = "local"` a clean merge gate lands the candidate on `main` with a
`--no-ff` merge, as it always has. With `mode = "pr"` the loop pushes the task
branch to `origin` instead and opens a pull request against `main` titled
`KO-n: TITLE`, whose body is the ticket body followed by the run's FINDINGS
entry, so the repository's own review bots and CI see the change before it
lands (design note 7). The loop then shepherds the pull request -- reads its
unresolved review threads and its checks, verdicts each thread, fixes and
replies, waits for CI -- for at most `pr_rounds` passes; see
[PR rounds](reviewing.md#pr-rounds) for the pass. A pull request that comes
up green with no unresolved thread is merged through the PR's merge API
under `approve = "auto"`, and under `approve = "human"` the run parks
exactly as above -- phase `awaiting_merge_approval`, ticket
`blocked_on_operator`, branch and worktree preserved, lease released -- with
the PR's URL recorded on the run (`runs.prUrl`), in the ticket's question
(`PR open: URL`, with the open threads listed) and in the ledger comment,
until `--approve KO-n` releases it: the resumed claim shepherds the PR once
more and merges it through the API when it is green and quiet. A declined
thread, a thread only a person can answer, red checks, or the cap park the
run the same way; `--shepherd KO-n` sends such a run back for another round
of passes without saying "merge". The factory never moves local `main` under
this mode: the merge is GitHub's, and the writer host's checkout tracks
`origin` by the operator's hand. The pull request is opened through `gh`
when it is on PATH, and otherwise through the GitHub API with a token read
from `GH_TOKEN` or `GITHUB_TOKEN` in the environment; the shepherd's calls
(`gh api`, GraphQL for the threads) take the same route, and the token is
never written to the config, the store or a log. Startup checks the route
before anything is claimed: a target with no `origin` remote, a `gh` whose
`gh auth status` fails, or neither `gh` nor a token is a startup error naming
`[merge] mode`. At the gate, a refused push, a PR create that fails, or a
route that has gone missing since startup ends the run as an infra failure
-- no strike against the ticket, branch and worktree preserved, and no pull
request recorded that was not opened. The value must be `"local"` or `"pr"`;
anything else is a startup error naming the key. "The factory never pushes"
is, under this mode, "the factory never pushes `main`".

`pr_rounds` is an integer of at least 1 (default 5): the number of shepherd
passes the loop makes over one pull request in one run before it parks the
run for the operator naming the cap, with the threads still open listed --
the cap that keeps the loop from arguing with a review bot forever. Every
pass is a `reviewRounds` row with route `github:LOGIN`, so the count is
visible in FINDINGS. Anything that is not such an integer (`0`, `true`,
`"5"`) is a startup error naming the key.

`pr_merge_method` is the `merge_method` the shepherd sends GitHub's merge API
when it lands a green, quiet pull request under `mode = "pr"`: `"merge"` (the
default) asks for a merge commit, like the local `--no-ff` merge; `"squash"`
and `"rebase"` are for a repository whose branch ruleset allows squash merges
only or requires linear history, which refuses a merge commit after every
gate has passed. The sha recorded on the run and in the ledger is the one
GitHub answers, which for `"squash"` and `"rebase"` is the new commit on
`main`. The key is validated whatever the mode; anything but the three
strings is a startup error naming the key. The local mode's `--no-ff` merge
is unaffected.

`pr_text` is where a pull request's title and body come from under `mode =
"pr"`. `"ticket"` (the default) is the form above: the title `KO-n: TITLE`,
the body the ticket verbatim with the run's FINDINGS entry appended. With
`"written"`, after the candidate is approved and verified and before the
branch is pushed, the loop runs one more turn on the implementer route in the
task worktree, given the diff against `main` (capped, with a note when cut),
the ticket body, the repository's `AGENTS.md` and `CLAUDE.md` when the
repository root has them, and `pr_style`. The turn answers with one line
`TITLE: ...` and the description in Markdown after it; the loop takes the
title as given, appends one line `Linear: KO-n` with the issue's URL to the
body, and opens the pull request with them. No FINDINGS entry is appended,
and the branch keeps its identifier. A reply with no `TITLE:` line, an empty
title or a title over 120 characters, or a turn that runs out of its budget
(a few minutes of the run's remaining box), falls back to the ticket form for
that pull request and prints one line saying so, so a pull request is always
opened. The text is written once, when the pull request opens; later shepherd
passes leave it alone. The value must be `"ticket"` or `"written"`; anything
else is a startup error naming the key.

`pr_style` is an optional string of instructions the written turn is given
verbatim, for the repository's own pull request conventions -- for example,
"Title starts with [Feature Name], the feature read from the diff. No ticket
identifier in the title. Describe what changed and why in a few short
paragraphs; no testing plan." It is read under `pr_text = "written"` only,
and anything but a string is a startup error naming the key.
