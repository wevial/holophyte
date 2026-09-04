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
(`[agents]`, `[worktree]`, `[supervisor]`, `[loop]`, `[report]`, `[board]`), a key it does
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
```

Accepted keys: `implementer`, `reviewer`, `adjudicator`.

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
```

Accepted keys: `setup`, `setup_timeout_sec`, `branch_prefix`.

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
# Whether the loop starts a detached --supervise for the target at startup
# when no live supervisor holds its lock. Optional; the default is true.
spawn_supervisor = true  # false: a service manager runs the supervisor
```

Accepted keys: `stop_on_failure`, `order`, `spawn_supervisor`.

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

With `spawn_supervisor = true` (the default) the loop checks the target's
`supervisor.lock` at startup, after the config and route checks and before
its first claim, and when no live pid holds it starts `factory.py --supervise`
for the same target as a detached process, logging to `supervisor.log` in the
state directory; when a live supervisor holds the lock it names that pid and
carries on. `spawn_supervisor = false` skips the check and the spawn, for an
operator whose service manager runs the supervisor as a unit of its own; the
explicit `--supervise` command is unchanged either way. A boolean, checked
like `stop_on_failure`.

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
`--report`, `--serve` and a read-only `--sweep` need no board and run without
the table; the loop and `--supervise` exit at startup naming `[board]
project_id` when it is absent. Nothing in the environment stands in for the
table.

```toml
[report]
# What the factory prints where it would print the machine's hostname.
# Optional; absent, the hostname is printed as recorded.
host_label = "writer-1"
```

Accepted keys: `host_label`.

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

