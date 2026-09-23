# Config

Every `config.toml` table the factory reads, with a commented example of each.
A ticket that adds a config table edits this file; one that adds a mode edits
the README's usage block. Back to the [README](index.md).

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
(`[agents]`, `[verify]`, `[worktree]`, `[supervisor]`, `[loop]`, `[report]`, `[board]`, `[merge]`, `[console]`, `[serve]`), a key it does
not read is
a startup
error naming the file, the table, the key and the keys the table accepts:
`setup_timeout_min` is a typo, not a timeout, and a typo the factory ignored
would leave a knob believed set that is not. The accepted keys are listed with
each table below.

## `[agents]`

A configured `writer` uses the same host-command treatment as a configured
adjudicator: its wrapper is responsible for enforcing read-only access. The
writing prompt asks for text only, leads with behaviour and reasons, and applies
the target's `pr_style` afterwards. Without a writer, writing keeps the
implementer's command and isolation settings.

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `implementer` | Default: Claude Code / Opus, high effort | Non-empty command string, or the table `[agents.implementer]` with `harness` (`"claude"`) and optional `model` and `effort` (default `"opus"`, `"high"`); override to select another implementer harness. A table's adapter builds the argv, records the session id at dispatch and builds the resume argv. |
| `reviewer` | Default: Hardened Codex review container | Non-empty command string, or the table `[agents.reviewer]` with `harness` `"codex"` and optional `model` and `effort` (default `"gpt-5.6-sol"`, `"medium"`; effort one of `"low"`, `"medium"`, `"high"`, `"xhigh"`), or `harness` `"devin"` with a required `model` and no `effort`; override only to supply an independent review route outside the container. |
| `adjudicator` | Default: Hardened Codex review container | Non-empty command string, or the table `[agents.adjudicator]` as for `reviewer`; change to supply a separate adjudication route. |
| `writer` | Default: Active implementer route | Non-empty command string for PR titles, descriptions and fix-round refreshes. Probed at startup; a failed probe is reported and writing uses the implementer. |
| `review_model` | Default: `"gpt-5.6-sol"` | Non-empty Codex model ID; change for a different container review model. |
| `review_effort` | Default: `"medium"` | `"low"`, `"medium"`, `"high"`, `"xhigh"`; change the container review reasoning effort. |
| `implementer_isolation` | Default: `"none"` | `"container"` isolates turns and live probes. Optional table form: `{ backend = "container", memory = "4g", writable = true }`; memory is a positive integer with `m` or `g` suffix; writable controls the workspace mount. |
| `implementer_image` | Default: reviewer image (`review_runner.IMAGE`) | Image containing the exact configured implementer CLI and target toolchain. Startup refuses a missing image and prints its build command. |
| `implementer_credential` | Default: `{}` (no credential) | Either `{ env = "AGENT_API_KEY" }` to pass one named host variable, or `{ file = "~/.agent/auth.json", destination = "/home/implementer/.agent/auth.json" }` to mount one regular file read-only under the temporary home. |
| `implementer_resume` | Default: absent (disabled) | Command string containing `{session}`; the findings prompt is appended as the last argv element. Refused beside a table implementer, whose adapter builds the resume. |
| `implementer_session` | Default: absent (disabled) | Regular expression string with exactly one capture group containing the session id. Refused beside a table implementer, whose adapter assigns the session. |
| `budget_scale` | Default: `1.0` | Finite number from 1.0 to 3.0; increase for a slower implementer harness. |
| `implementer_fallback` | Default: Absent (disabled) | Non-empty command string distinct from the primary; set for a probed backup implementer. |
| `reviewer_fallback` | Default: Absent (disabled) | Non-empty command string distinct from the primary; set for a probed backup reviewer. |
| `adjudicator_fallback` | Default: Absent (disabled) | Non-empty command string distinct from the primary; set for a probed backup adjudicator. |

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
# Multiplier on the implementer turn's wall-clock budget: the ticket's
# estimate times this is the cap each implementer turn is armed with, the
# box the sweep and /status count the run against, and the hard ceiling the
# thirty-minute cap scales to. Optional; 1.0 when absent, a number from
# 1.0 to 3.0 otherwise. Set it for a slower harness, not for a bigger task.
budget_scale = 1.5
```

A role can instead be a table naming a harness adapter in
`holophyte/harness.py`. Only `implementer`, `reviewer` and `adjudicator` may
be tables, and only for a role the harness supports; today that is `claude`
for `implementer` and `codex` or `devin` for `reviewer` and `adjudicator`.
Unknown keys, an unknown harness, a role the harness does not serve, or an
option the harness requires or refuses are startup errors. `[agents.implementer] harness = "claude"`
runs `claude -p --session-id U --model M --effort E PROMPT` with a fresh UUID
`U`, records `U` on the run before launch (a turn the budget kills keeps it),
and resumes with `claude -p --resume U --model M --effort E PROMPT`. The
binary is `claude` on PATH, or the absolute path in the top-level
`[harnesses]` table. Under container isolation the image supplies the bare
`claude`, `[harnesses]` is ignored and no session is recorded.

```toml
[agents.implementer]
harness = "claude"
model   = "opus"    # optional; passed to --model
effort  = "high"    # optional; passed to --effort as written

[harnesses]
claude = "/opt/claude/bin/claude"   # optional; absolute path only
```

`[agents.reviewer] harness = "codex"` (and the same for `adjudicator`) does
what a host wrapper script used to. Each turn runs in a throwaway detached
worktree of `refs/review/RUN/candidate` inside the review scratch directory,
removed with it on every exit, as
`codex exec -m M -c model_reasoning_effort=E
--dangerously-bypass-approvals-and-sandbox PROMPT`: Codex's read-only sandbox
cannot start under a systemd user unit with PrivateTmp, so the throwaway
checkout is the write boundary. The id from Codex's first `session id:` line
is written to `$HOLOPHYTE_REVIEW_SCRATCH/session`, and when
`HOLOPHYTE_REVIEW_RESUME` is set (see `[loop] review_session`) the turn is
`codex exec resume` with the same options, the id and the prompt; a resume
answered with "no rollout found" runs once more fresh and records a
`review_session` event with that `reason`. `review_model` and `review_effort`
beside a table reviewer are refused like beside a command.

```toml
[agents.reviewer]
harness = "codex"
model   = "gpt-5.6-sol"   # optional; passed to -m
effort  = "medium"        # optional; low, medium, high or xhigh
```

`[agents.reviewer] harness = "devin"` (and the same for `adjudicator`) runs
in the same throwaway candidate checkout as
`devin --model M --permission-mode dangerous --respect-workspace-trust false
-p PROMPT`. Print mode fails in a directory Devin has never trusted, and every
throwaway checkout is one; `dangerous` lets the reviewer run git and tests
without a prompt, the checkout staying the write boundary. After the turn,
`devin list --format json` in that checkout, which holds only this turn's
session, supplies the id written to `$HOLOPHYTE_REVIEW_SCRATCH/session`; any
other answer records none. A resume adds `-r ID` before `-p`. `model` is
required, since the factory has no Devin default, and `effort` is refused,
since the CLI has no effort flag.

```toml
[agents.reviewer]
harness = "devin"
model   = "opus"          # required; passed to --model
```

Container implementation uses the reviewer hardening flags, a 4 GiB memory cap,
bridge networking, the factory user's non-root UID/GID, a temporary home and
`/workspace` mounted read-write. Only `[worktree] env_allow` values and the
declared credential enter the agent environment, alongside fixed runtime and Git
identity settings. With no worktree environment configured, no host environment
variables are inherited. Git author identity comes from the target's configured
`user.name` and `user.email`; host Git configuration and hooks are not mounted.
A self-contained Git directory permits commits in linked worktrees; objects,
HEAD and index return to the host after the container has been removed. Verify
and capture commands still run on the host. The image must supply the CLI;
host executables are not mounted. The reviewer image alone may need extending
for the configured implementer. `none` preserves existing host behavior.

Set `[loop] fix_session = "resume"` to reuse the recorded session for review
fix rounds, with e.g. `implementer_resume = "codex exec resume {session}"`
in `[agents]` (include the model and sandbox flags for your implementer),
or with a table implementer, whose adapter builds the resume argv itself.
The resumed prompt contains reviewer findings and adjudication instructions;
the original ticket is already in the session. `alternate` assigns odd store
run ids to resume and even ids to fresh, consistently across their fix rounds.
Resume requires this run's session id, a configured template (or a table
implementer) and the primary implementer route. Otherwise the ordinary fresh prompt is used. A nonzero
resume exit without a new commit gets one fresh retry, subject to the run
budget; a timeout remains a budget failure. Resume launch errors also retry
fresh once. With either experimental setting, each fix round records a
`fix_session` run event with `arm`, `resumed`, and a `reason` when skipped or
failed. The default `fresh` setting records no such events. These settings
apply to review fix rounds only; pull request babysitting is unchanged.

Set `implementer_session` to extract a session handle from captured implementer
output (stdout and stderr), for example the Codex banner:

```toml
[agents]
implementer_session = 'session id: ([0-9a-f-]{36})'
```

After each implement or fix turn, including a timed-out turn, the first match
updates `runs.providerSessionId` and appends an `agent_session` detail event.
Its payload contains `session_id`, `role` (`implement`) and `route`
(`primary` or `fallback`). Later ids replace the column while the events
retain history in order. With no pattern or no match, nothing is recorded.
Invalid regular expressions or a capture-group count other than one are refused
at startup. Writer, reviewer, adjudicator and container-isolated turns are
excluded. This records handles only; it does not resume sessions.

`budget_scale` exists because the budget stops runaway turns, not because
it selects a harness: an implementer that reads more and edits later can
hit a thirty-minute cap on work it had nearly finished, and the answer is
more wall clock, not a smaller ticket. The estimate, the ticket and the
template's ceiling are untouched — the scale multiplies them only where the
clock is armed, so a `budget_scale = 1.5` target arms a 45-minute turn for
a 30-minute ticket, the timeout line names both figures, and the
supervisor's time-box sweep allows for the scaled box. A value under 1.0
or over 3.0 — or a non-number — is a startup error naming the key and the
range.

A configured `implementer` is probed before each pass claims a ticket: the
loop runs the exact command a turn would, with the goal `Reply with the single
word: ready`, in an empty temporary directory under a 90 s cap, and the pass
proceeds only when it exits 0 with `ready` in its output. A route that exits
nonzero, answers something else or does not answer in time ends the pass nonzero
with the command, the exit code or the timeout, and the last lines it printed --
a typo or a stale CLI is found here, not by a failed implement turn later. The
default route is also probed when it has a fallback. Review seats with a fallback
are also probed, in a temporary checkout so review wrappers can resolve refs. The probe
runs when a pass starts and again when the daemon's `PUT /config` (behind
`[serve] config_edit` and the write token, never the read token alone: the
probe executes whatever command the key names) changes this key: the write
lands either way, and the reply's `probe` carries the same verdict, command,
exit code and last lines the loop would print, so a route that does not answer
is known at the write and not at the next `factory.py` start.

Each seat may name a fallback command, for example:

```toml
[agents]
implementer = "codex exec --model gpt-5.6-sol"
implementer_fallback = "devin -p"
```

Fallbacks use the same command-string grammar and receive the goal as the last
argument. A fallback must differ from its primary. The loop probes the fallback
when the primary probe fails, or when a turn emits its route's quota signature.
Only a successful fallback probe activates the route; the interrupted turn is
then dispatched once on it. The seat stays on fallback for the process's remaining
turns and retries its primary at the next start. Each switch prints its reason
and command, records a `route_fallback` project intervention and run event (a
startup switch attaches to the first affected run), and adds a fallback chip to
the console's project header. A failed fallback probe stops the loop without
recording a switch. `review_model` and `review_effort` cannot accompany fallback
keys, just as they cannot accompany `reviewer`.

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
Executable resolution is separate from the live route probes described above.
Relative paths with a directory in them (`./review.sh`) are refused: rounds run
in a task worktree that does not exist yet, so the name would resolve somewhere
neither startup nor the operator named.

## `[harnesses]`

Where a harness adapter finds its binary when a role in `[agents]` is written
as a table. Keys are registered harness names; each value is an absolute
path. Absent, the adapter runs the harness's own name from PATH. Ignored for
the implementer under `implementer_isolation = "container"`, where the image
supplies the binary; review roles run on the host and keep their path.

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `claude` | Default: `claude` on PATH | Absolute path to the Claude CLI; set when the binary the factory should run is not the first `claude` on PATH. A relative path is refused. |
| `codex` | Default: `codex` on PATH | Absolute path to the Codex CLI for a table reviewer or adjudicator; set when the binary the factory should run is not the first `codex` on PATH. A relative path is refused. |
| `devin` | Default: `devin` on PATH | Absolute path to the Devin CLI for a table reviewer or adjudicator; set when the binary the factory should run is not the first `devin` on PATH. A relative path is refused. |

## `[loop]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `stop_on_failure` | Default: `true` | Boolean; set false to continue the queue after a failed run. |
| `order` | Default: `"identifier"` | `"identifier"` or `"priority"`; choose priority to claim urgent Linear tickets first. |
| `spawn_supervisor` | Default: `true` | Boolean; disable when a service manager owns the supervisor. |
| `review_rounds` | Default: `2` | Integer at least 1; change the base independent review allowance. |
| `review_rounds_per_lines` | Default: `800` | Integer at least 0; change the diff-size scaling interval, or use 0 to disable scaling. |
| `review_rounds_max` | Default: `4` | Integer at least 1 and at least review_rounds; change the scaled round ceiling. |
| `workers` | Default: `1` | Integer at least 1; increase to work multiple claimable tickets concurrently. |
| `review_session` | Default: `"fresh"` | `fresh`, `resume`, or `alternate`; alternate requests reviewer resume on odd run ids, fresh on even run ids. |
| `fix_session` | Default: `"fresh"` | `fresh`, `resume`, or `alternate`; alternate resumes odd run ids and starts even run ids fresh. |
| `tick_sec` | Default: `120` seconds | Integer at least 10; change how soon a pool with spare slots notices new work. |

Configured reviewer wrappers may write their session id to
`$HOLOPHYTE_REVIEW_SCRATCH/session` before exiting. The file must contain an
opaque non-empty string of at most 200 characters with no whitespace (including
no trailing newline). Missing, unreadable or invalid ids are silently ignored.
The factory reads it before removing the scratch directory and records an
`agent_session` event with `session_id`, `role`, `route`, and review `round`.
It does not replace the implementer's recorded session.

With `review_session = "resume"`, round two and later on the primary route
receive round one's primary reviewer id in `HOLOPHYTE_REVIEW_RESUME`. The
wrapper is responsible for resuming that session, including across its
throwaway checkout directories. The full review prompt is still passed.
The first round, fresh arm, missing id, and fallback route receive no resume
variable. Each re-review in the experiment records a `review_session` event
with `arm`, `requested`, and a `reason` when no resume is requested. The default
`fresh` setting emits no such experiment event. The default container reviewer,
PR thread reviews, covering review, and adjudicator are outside this experiment.
The reviewer remains independent of the implementer in every arm.

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

By default one failed run ends the process after its close-out, with a nonzero
exit, and an operator relaunches the loop — the right call while the loop is
still being watched. With `stop_on_failure = false` the run is closed out
exactly as before (released, escalated if it was one failure too many, the
`FINDINGS.md` window regenerated under `[report] findings = "repo"`) and the
loop goes on to the next ready ticket
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

## `[board]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `project_id` | Default: None; required for a configured board | Non-empty string naming the Linear project UUID; set to choose the target's queue. |
| `team` | Default: None; required for a configured board | Non-empty string naming the Linear team; set to resolve that team's workflow states. |
| `label` | Default: Absent (no filter) | Non-empty string; set to claim only ready issues with this label. |

```toml
[board]
# The Linear project this target claims from and the team whose workflow
# states its tickets move through. Required for the loop and --supervise.
project_id = "00000000-0000-0000-0000-000000000000"
team = "Example Team"
# The label a ready issue must carry for the loop to see it. Optional;
# absent, every ready issue in the project is the loop's.
label = "holophyte"
```

The board is a per-target setting: two targets on one host driven from one
process-wide variable would both claim from the same project, and the second
would silently work the first's queue. Both values must be non-empty strings.
`--report`, `--serve`, `--repoint` and a read-only `--sweep` need no board and
run without the table; the loop and `--supervise` exit at startup naming `[board]
project_id` when it is absent. Nothing in the environment stands in for the
table.

`label` is the opt-in for a project people also work in: when it is set, the
loop's ready listing keeps only issues carrying that label by name
(case-sensitive, as Linear shows it), so a ticket a person has decided to
take, or one that is a plan rather than a contract, is invisible to the
factory however ready it looks. The claim, the queue the console's Board
mirrors and the supervisor's board fallback all read through the same filter,
so the label decides the whole queue rather than only the claim. The key is
read once when the process builds its provider: set, unset or changed on a
live target it takes effect at the next restart, not the next pass. The board
side is live -- the loop asks the board fresh each claim, so a ticket that
gains the label joins the listing, and a mirror row whose ticket lost it
waits on the board at `blocked_on_deps` until it carries it again.
`project_id` and `team` are required when the table is present; `label` is
the one key that may be absent, but when written it must be a non-empty
string, and anything else is a startup error naming the key.

## `[worktree]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `setup` | Default: `[]` | List of non-empty shell command strings; set to install the target's dependencies before agent turns. |
| `setup_timeout_sec` | Default: `300` seconds | Finite positive number; increase for slower dependency installation. |
| `branch_prefix` | Default: `"task"` | Legal single git branch segment (constraints below); change to follow the target's branch naming convention. |
| `env_source` | Default: absent | Source dotenv path, with `~` expanded; relative paths resolve beside config.toml. Requires `env_allow`. |
| `env_allow` | Default: absent | List of names matching `[A-Za-z_][A-Za-z0-9_]*`; requires `env_source`. Missing names refuse startup. Writes exactly these assignments to a mode-0600 `.env` before setup commands. An empty list writes an empty file. |
| `carry` | Default: `[]` | List of non-empty repository-relative directory paths without `..`; set for ignored dependencies the reviewer needs. |

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

`env_source` accepts `NAME=value` lines, blank lines, comments and an optional
`export ` prefix. Values (including quotes) are kept verbatim; no shell
expansion runs. Duplicate names use the last assignment. Invalid assignments
are refused by line number without revealing values. Source values are
redacted from loop output, events, ledger narratives, run failure reasons and
review verification results. CRLF line endings are accepted. Setup refuses a
tracked `.env` and adds `/.env` to Git’s local `info/exclude` if needed. Factory
recovery staging excludes it independently of ignore rules; a candidate with
`.env` in its tree or new history cannot be pushed. With neither key, setup
writes no environment file.

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

## `[verify]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `always` | Default: `[]` | List of non-empty shell command strings; set fast baseline checks required at every verify gate. |
| `before_merge` | Default: `[]` | List of non-empty shell command strings; set additional, expensive checks needed at the merge gate. |
| `timeout_sec` | Default: `300` seconds | Finite positive number; increase the per-command baseline timeout for slower checks. |

The default empty tiers add no checks. Ticket verify commands run first,
then `always`; at the merge gate `before_merge` runs last. Commands run in
order in the task worktree and stop at the first failure. These baselines
supplement the ticket's exact commands; `timeout_sec` applies to baseline
commands, not to the ticket's commands or worktree setup.

Every line of an instrumented simple newline verify block must pass, and the
run stops at the first failure. Parseable top-level `&&` chains also stop at
the first failing clause; both forms report the failed clause and exit status.
This applies to ticket commands and to blocks in both `always` and
`before_merge`. Lines share one shell, preserving exported variables and
`cd`; append `|| true` to a line whose failure is explicitly allowed. Complex
shell programs (including semicolon-separated command lists) execute verbatim,
with their own shell exit semantics and without clause-level reporting or
the instrumented first-failure guarantee.

```toml
[verify]
always = ["ruff check holophyte tests store"]
before_merge = ["python3 -m unittest discover -s tests"]
timeout_sec = 300
```

See [the loop](loop.md) for verify points and automatic `.githooks` enablement.

## `[merge]`

Before pushing a task branch or merging it locally, the factory removes matching
attribution lines and their leftover blank lines from unpublished commits. The
defaults match AI `Co-Authored-By:` trailers naming Claude, Devin, Codex, Copilot
or Cursor, or containing `noreply@anthropic.com` or `devin-ai-integration`;
`Generated with` lines linking to those agents' sites; and the
`🤖 Generated with` form. Human co-author trailers remain. Before a rewrite, remote refs are
refreshed; commits reachable from origin or main are never rewritten.
The rewrite preserves trees, authors and dates, leaves the working tree and
index alone, and checks the tip tree before atomically moving the branch.
Signatures on rewritten commits are removed because they no longer validate.
Invalid regular expressions refuse startup, naming `strip_attribution`.
Use TOML literal strings for custom patterns, for example
`strip_attribution = ['(?i)^Generated by internal bot\b']`.


| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `approve` | Default: `"auto"` | `"auto"` or `"human"`; choose human to require an explicit operator approval before merging. |
| `mode` | Default: `"local"` | `"local"` or `"pr"`; choose pr to send the candidate through GitHub checks and review. |
| `pr_rounds` | Default: `5` | Integer at least 1; change the maximum babysit passes before parking. |
| `pr_merge_method` | Default: `"merge"` | `"merge"`, `"squash"`, or `"rebase"`; match the repository's merge rules. |
| `pr_poll_sec` | Default: `180` seconds | Integer at least 10; change the minimum interval between automatic babysit resumes. |
| `pr_quiet_sec` | Default: `300` seconds | Integer at least 0; change the quiet period after GitHub activity, or use 0 for immediate green merges. |
| `strip_attribution` | Default: agent attribution patterns | List of Python regular expressions searched per commit-message line; `[]` disables cleanup. Replaces the default patterns when set. |
| `check_wait_sec` | Default: `1800` seconds | Integer at least 1; increase the pending-check and quiet-period wait cap for slow CI. |
| `pr_changes_log` | Default: `false` | Boolean; set to `true` to retain an ordered "Changes since first review" list across approved fix rounds. Otherwise the next successful description refresh removes any existing list, preserving Evidence, the Linear line and appended blocks. Non-boolean values are a startup error naming the key. |
| `pr_style` | Default: `""` | String; set instructions for the title and description writer to follow repository conventions. |
| `ui_paths` | Default: `[]` | List of non-empty repository-relative globs without `..`; set with ui_capture to identify changes needing visual evidence. |
| `ui_capture_dir` | Default: `"e2e/capture"` | Directory named in the implementer brief for ticket capture scripts. |
| `ui_capture` | Default: `""` | Command string with shell-style quoting but no shell evaluation; set with ui_paths to capture evidence non-interactively. |
| `capture_env_source` | Default: absent | Source dotenv path for the capture command only, with `~` expanded; relative paths resolve beside config.toml. Requires `capture_env_allow`. |
| `capture_env_allow` | Default: absent | List of names matching `[A-Za-z_][A-Za-z0-9_]*`; requires `capture_env_source`. Missing names refuse startup, naming the variable. Exactly these values are added to the `ui_capture` command's environment, on the host and in a container; they are never written to the worktree and never reach agent turns or verify commands. Source values are redacted from output. |
| `media_repo` | Default: `""` (target repository) | Empty string or GitHub `owner/name`; set a separate repository to keep evidence out of the target's git storage. |
| `media_bucket` | Default: Absent (git publishing) | Table described under [merge.media_bucket](#mergemedia_bucket) below; set to publish evidence in S3-compatible object storage instead. |
| `media_max_file_mb` | Default: `10` MB | Finite positive number; change the largest permitted individual evidence file. |
| `media_max_total_mb` | Default: `20` MB | Finite positive number; change the total evidence budget per capture. |
| `human_threads` | Default: `"park"` | `"park"` or `"act"`; choose act to judge and fix concrete human requests while leaving their threads unresolved. |
| `bot_threads` | Default: `"act"` | `"act"` or `"advisory"`; choose advisory to record unmentioned bot findings without blocking the merge. |
| `bot_logins` | Default: `[]` | List of login strings; set to classify additional bot accounts for advisory routing and human-reply escalation. |
| `mention_accounts` | Default: `[]` | List of GitHub login strings, matched case-insensitively; only these accounts may give mention instructions. Empty or absent keeps mentions open to any account; startup logs this once when `human_threads = "act"`. |
| `mention_handle` | Default: `"holophyte"` | String, written without `@`; change the handle used for direct instructions in threads and the PR conversation. |
| `after` | Default: `[]` | List of shell command strings; set post-merge builds or other commands needed in the main checkout after a local merge. |
| `bot_authors` | Default: `["devin-ai-integration", "coderabbitai", "greptile-apps", "github-actions"]` | List of login strings replacing these defaults; change which bots' declined threads are resolved (the `[bot]` suffix still qualifies). |

```toml
[merge]
# Who says "merge" once the reviewer has approved and the pre-merge verify
# has passed. Optional; the value shown is the default.
approve = "auto"   # "human": park the approved run for an operator to release
# Where an approved, verified candidate goes. Optional; the value shown is
# the default.
mode = "local"     # "pr": push the branch to origin and open a pull request
# How many babysit passes over an open pull request before the run parks
# for the operator. Optional; the value shown is the default.
pr_rounds = 5
# How the babysitter merges a green, quiet pull request: "merge", "squash" or
# "rebase". Optional; the value shown is the default.
pr_merge_method = "merge"
# The least seconds between two babysit rounds the loop itself starts on
# one parked pull request when it sees new review activity. Optional; the
# value shown is the default.
pr_poll_sec = 180
# The least seconds a pull request must have been green and untouched --
# no comment, review, push or check -- before the babysitter merges it.
# Optional; the value shown is the default. 0 merges as soon as it is
# green.
pr_quiet_sec = 300
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

With a non-empty `mention_accounts` list, unlisted mentions receive one reply per
thread: "Only listed maintainers may instruct the factory here", with the standard
comment header. Review threads retain normal bot or human routing; conversation
comments yield no instruction. Set `mention_accounts = ["maintainer-login"]`
to restrict instructions in both places. A bare string or a non-string list entry
is rejected at startup.

`mention_handle` defaults to `"holophyte"` (without `@`). A review thread's
latest comment mentioning `@holophyte`, case-insensitively, is an instruction:
the factory fixes it without adjudication, replies with the commit SHA, and
resolves it. Set `mention_handle = "factory-bot"` to use `@factory-bot` instead.
Earlier mentions do not make a thread an instruction. Unmentioned threads
retain the `human_threads` and `bot_threads` policies.

`bot_authors` is a list of login strings, defaulting to
`["devin-ai-integration", "coderabbitai", "greptile-apps", "github-actions"]`.
A declined thread whose opening author is listed or whose login ends in
`[bot]` is replied to with the reason and then resolved. Other declines stay
open and park the run. Setting the list replaces the defaults; `[]` keeps
only the suffix rule. A bare string or a non-string entry fails startup
naming `bot_authors`. This key controls decline resolution; the existing
GitHub author-type policy still determines which threads are adjudicated.

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
branch to `origin` instead and opens a pull request against `main` with
a title and body written from the change, so the repository's own review
bots and CI see the change before it lands (design note 7). The loop then babysits the pull request -- reads its
unresolved review threads and its checks, verdicts each thread, fixes and
replies, waits for CI -- for at most `pr_rounds` passes; see
[PR rounds](reviewing.md#pr-rounds) for the pass. A pull request that comes
up green with no unresolved thread is merged through the PR's merge API
under `approve = "auto"`, and under `approve = "human"` the run parks
exactly as above -- phase `awaiting_merge_approval`, ticket
`blocked_on_operator`, branch and worktree preserved, lease released -- with
the PR's URL recorded on the run (`runs.prUrl`), in the ticket's question
(`PR open: URL`, with the open threads listed) and in the ledger comment,
until `--approve KO-n` releases it: the resumed claim babysits the PR once
more and merges it through the API when it is green and quiet. A declined
thread left open for its author, a thread only a person can answer, red
checks, or the cap park the run the same way; `--babysit KO-n` sends such a run back for another round
of passes without saying "merge". The factory never moves local `main` under
this mode: the merge is GitHub's, and the writer host's checkout tracks
`origin` by the operator's hand. The pull request is opened through `gh`
when it is on PATH, and otherwise through the GitHub API with a token read
from `GH_TOKEN` or `GITHUB_TOKEN` in the environment; the babysitter's calls
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

`pr_rounds` is an integer of at least 1 (default 5): the number of babysitter
passes the loop makes over one pull request in one run before it parks the
run for the operator naming the cap, with the threads still open listed --
the cap that keeps the loop from arguing with a review bot forever. Every
pass is a `reviewRounds` row with route `github:LOGIN`, so the count is
visible in FINDINGS. Anything that is not such an integer (`0`, `true`,
`"5"`) is a startup error naming the key.

`pr_merge_method` is the `merge_method` the babysitter sends GitHub's merge API
when it lands a green, quiet pull request under `mode = "pr"`: `"merge"` (the
default) asks for a merge commit, like the local `--no-ff` merge; `"squash"`
and `"rebase"` are for a repository whose branch ruleset allows squash merges
only or requires linear history, which refuses a merge commit after every
gate has passed. The sha recorded on the run and in the ledger is the one
GitHub answers, which for `"squash"` and `"rebase"` is the new commit on
`main`. The key is validated whatever the mode; anything but the three
strings is a startup error naming the key. The local mode's `--no-ff` merge
is unaffected. For `"squash"` and `"merge"`, the commit subject is the pull
request title followed by ` (#N)` and the commit body is its first Summary
paragraph (empty when absent). `"rebase"` sends neither override.

`pr_poll_sec` is the least time, in seconds, between two babysit rounds the
loop itself starts on one parked pull request (KO-362). Every tick already
reads each parked pull request once to notice a merge; the same read carries
GitHub's `updatedAt` and the review-thread count, and a pull request that
has moved past what the last babysit pass recorded on the run is sent back
to the babysitter as `--babysit KO-n` would send it (see [Loop](loop.md)) --
no more often than this per pull request, measured from the park, so a
reviewer typing three comments in a minute gets one round rather than
three. The default is 180; an integer of at least 10, and anything else
(`5`, `"180"`, `true`) is a startup error naming the key. The reads back off
on their own when the token's GraphQL budget runs low, whatever the value.

`pr_quiet_sec` is the least time, in seconds, a pull request must have been
green with no unresolved thread before the babysitter merges it (KO-429) --
the "quiet" of "green and quiet". It is measured from GitHub's `updatedAt`,
which moves on every comment, review, push and check: a reviewer still
typing, a bot's second pass not yet posted, or a commit pushed a minute
after the checks went green all restart the count. Until then the pass
re-reads the pull request on the cadence it uses for pending checks and
prints how long it has been quiet of the quiet required, and `pr_rounds`
still caps a pull request that never goes quiet. The default is 300; an
integer of at least 0, and anything else (`-1`, `"300"`, `true`) is a
startup error naming the key. `0` is the merge-as-soon-as-green the
babysitter had before.

Pull request bodies are always written. After the candidate is approved and
verified and before the branch is pushed, the loop runs one more turn on
the implementer route in the task worktree, given the diff against `main`
(capped, with a note when cut),
the ticket body, the repository's `AGENTS.md` and `CLAUDE.md` when the
repository root has them, the repository's pull request template --
`.github/pull_request_template.md`, or `PULL_REQUEST_TEMPLATE.md` under
`.github/` or at the root -- when the worktree has one, with the instruction
to fill its sections, and `pr_style`. The turn answers with one line
`TITLE: ...` and the description in Markdown after it; the loop takes the
title as given, appends one line `Linear: KO-n` with the issue's URL to the
body, and opens the pull request with them. No FINDINGS entry is appended,
and the branch keeps its identifier. A reply with no `TITLE:` line, an empty
title or a title over 120 characters, an empty body, or a turn that runs out
of its budget (a few minutes of the run's remaining box), falls back to the
ticket title and a short stub. The stub contains the first paragraph of the
ticket's Summary section (up to 600 characters), one line saying
`The description could not be written: REASON`, and the `Linear: KO-n` line
with the issue URL. The loop prints the failure reason. The description is rewritten after each approved fix round from the current
diff, ticket and repository conventions; see [PR rounds](reviewing.md#pr-rounds).

`pr_style` is an optional string of instructions the written turn is given
verbatim, for the repository's own pull request conventions -- for example,
"Title starts with [Feature Name], the feature read from the diff. No ticket
identifier in the title. Describe what changed and why in a few short
paragraphs; no testing plan." Anything but a string is a startup error
naming the key.

Visual evidence is captured when the diff matches `ui_paths`. Configure it
together with `ui_capture`; leaving both absent leaves PRs unchanged. The
capture command receives one output-directory argument and has five minutes
to write PNG, WebM or MP4 files. Evidence is shared with review prompts and
PR descriptions. A ticket may list up to six states under optional `## Evidence`,
one per line. With capture configured, the implementer is told to add or update
a script under `ui_capture_dir`, producing `01-slug.png`, `02-slug.png`, etc.
in state order, plus a recording when the states describe a flow. The command
receives `HOLOPHYTE_TICKET` and, only when states are listed,
`HOLOPHYTE_EVIDENCE_STATES` joined with newlines, plus any
`capture_env_allow` values. The target's own harness
selects and runs that ticket's script. Numbered images receive state captions;
missing images are marked "not captured" in the PR and reviewer prompt.
Tickets without the section keep the default capture.

`capture_env_source` and `capture_env_allow` supply credentials the capture
needs and the implementer does not, such as a server's API keys. They keep
those values out of the implementer's `.env`, environment and prompt, not out
of reach: with `implementer_isolation = "none"` the implementer runs as the
same host user and could read the source file, and the capture command runs
spec code the candidate wrote, which sees the values while it runs.

MB means 1,048,576 bytes: oversized files are omitted, then
videos are dropped first to fit the total cap, with each omission listed in
Evidence. Without a bucket, `media_repo` selects a separate GitHub repository;
otherwise evidence goes to the target repository on `pr-media/KO-n`. Image
links reflect the destination repository's visibility; videos use blob links.

A human mention in the pull request conversation tab is also an instruction,
bypassing adjudication. The factory answers with a conversation comment
quoting the request and naming the fix SHA; conversation comments have no
review thread to resolve. Unmentioned conversation comments are ignored.

## `[merge.media_bucket]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `endpoint` | Default: None; required when the table is present | HTTP(S) URL without credentials, query, fragment or whitespace; set the S3-compatible API endpoint. |
| `bucket` | Default: None; required when the table is present | Non-empty S3 bucket name using lowercase letters, digits, dots and hyphens; set the destination bucket. |
| `public_base` | Default: None; required when the table is present | HTTP(S) URL without credentials, query, fragment or whitespace; set the public URL prefix from which readers fetch objects. |
| `retention_days` | Default: Absent (displayed as "not specified") | Positive integer when set; set to describe the lifecycle policy you configured on the bucket. |

Bucket publishing takes precedence over both git publishers. Set
`HOLOPHYTE_MEDIA_ACCESS_KEY_ID` and `HOLOPHYTE_MEDIA_SECRET_ACCESS_KEY` in
the writer's environment, never in this file. The operator must configure
public reads and lifecycle expiry on the bucket; `retention_days` is display
metadata and does not install an expiry policy. The validator uses 1 as its
validation fallback when the key is omitted, but the publisher displays
"not specified" until the operator supplies a value.

```toml
[merge.media_bucket]
endpoint = "https://objects.example.com"
bucket = "review-evidence"
public_base = "https://media.example.com"
retention_days = 7
```

## `[supervisor]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `heartbeat_stale_min` | Default: `5` minutes | Finite positive number; increase for a target whose healthy heartbeat can be delayed. |
| `stale_strikes` | Default: `2` | Positive integer; increase to require more consecutive silent sightings before acting. |
| `budget_grace` | Default: `1.5` | Finite positive number; change the grace multiplier on the run's per-turn allowance. |
| `run_cap` | Default: `3.0` | Finite number from 1.5 to 5.0; change the hard ceiling in scaled ticket boxes. |
| `review_overlap_threshold` | Default: `0.5` | Finite number in (0, 1]; change the shared-findings fraction that signals a stuck review. |
| `sweep_interval_sec` | Default: `60` seconds | Finite positive number; change how often the supervisor sweeps. |
| `restart_grace_sec` | Default: `120` seconds | Finite positive number; increase for slower self-merge restarts. |
| `board_ask_sec` | Default: `600` seconds | Integer at least 60; change the minimum interval between fallback board listings. |

```toml
[supervisor]
# The sweep's thresholds. Every key is optional; the values shown are the
# defaults, in place whenever the key (or the whole table) is absent.
heartbeat_stale_min      = 5    # a heartbeat older than this is a silent sighting
stale_strikes            = 2    # consecutive silent sightings that trip a run
budget_grace             = 1.5  # multiple of the ticket's estimate that blows the box
run_cap                  = 3.0  # the run's hard ceiling, in boxes: the loop refuses a turn past it
review_overlap_threshold = 0.5  # findings shared by two rounds that reads as stuck
sweep_interval_sec       = 60   # sleep between two --supervise passes
restart_grace_sec        = 120  # how long a self-merge re-exec may take to come back
board_ask_sec            = 600  # least wait between two fallback asks of the board
```

`board_ask_sec` bounds how often the supervisor's board fallback may list
the board's ready tickets. The fallback runs when the mirror is empty and
no loop is live — a ticket filed while the loop was down has no mirror
row, so the pass asks the board itself — and a ready listing is Linear's
most expensive query here, thousands of the key's hourly complexity
points. The last ask is stamped on the project's store row, so the
interval holds across passes and across supervisor restarts whatever
`sweep_interval_sec` is. The value is an integer of at least 60; under a
minute the fallback is the polling that emptied the key.

Separately, every Linear answer carries the key's complexity-budget
headers and the provider shares low-budget deadlines per API key under
`HOLOPHYTE_HOME/linear-budget` (default `~/.holophyte/linear-budget`). When under
a tenth of the limit remains, the fallback and the loop's idle relisting
both wait for the reset instead of spending the points to be refused: one
`[holo2] board not asked: budget resets at HH:MM` line per refill, and no
calls until it. Supervisor loop starts and pool worker claims honor the same
deadline, including after a process restart; the pool also checks after
mirroring before spawning workers. If a low reading omits the reset, the
factory waits one hour from that reading before probing again. A refused
answer (HTTP 429) lands the same way, as a
`LinearBudgetExhausted` naming the reset.

The box is counted per turn: a run's allowance is the ticket's estimate once
for its first implementer turn and once more for each review round it has
recorded, up to the run's review cap, all under `budget_grace` -- the same
budget the loop gives each turn, so a fix round after a review is not swept as
overtime. A run with no review round yet is judged against the single box.

Whatever that allowance grows to, `run_cap` times the box is the ceiling: the
loop refuses to arm a turn whose budget would carry the run past it -- the run
fails there, candidate preserved, instead of the turn being killed mid-edit --
and a run that slips past anyway is swept. The ceiling exists for the run that
keeps earning turns by failing review, the case the per-turn budget cannot
bound. `run_cap` is a number from 1.5 to 5.0; `/status` carries it in
`thresholds` so the console's time-box bar can draw it.

Different targets want different patience — a Go build's worktree setup is
slower than stdlib Python's — and these are the knobs `--sweep` and
`--supervise` read. Each value is checked at startup, for every mode: the
thresholds and the interval must be positive numbers, `stale_strikes` a
positive integer, `board_ask_sec` an integer of at least 60, the overlap
a fraction in (0, 1], and `run_cap` a number
from 1.5 to 5.0. A value outside its
constraint is an error naming the key and the constraint, like malformed TOML,
rather than a default quietly used in its place. A key this version does not
know is refused the same way. The config is read once at startup; a running
supervisor does not pick up an edit.

## `[serve]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `transcripts` | Default: `[]` | Allowed transcript roots (a path or list of paths), relative to the config directory or absolute, with home expansion. Empty disables transcript reads; turn metadata remains available. Codex roots contain rollout JSONL files; durable Devin exports belong below a directory named for the session id. The operator must preserve review exports before scratch cleanup. |
| `token_file` | Default: Absent | Non-empty path string, relative to the config directory or absolute, with home expansion; set for non-loopback reads or any enabled write routes. |
| `machine_token_file` | Default: Absent | Non-empty path string, resolved as `token_file` is; set to accept one machine-wide token beside the project's own wherever `token_file` is demanded. |
| `actions` | Default: `false` | Boolean; enable to expose authenticated daemon action routes. |
| `config_edit` | Default: `false` | Boolean; enable to read and edit config through authenticated daemon routes. |
| `name` | Default: Target directory name | Non-empty string without `/`; change to match the deployed systemd instance. |

```toml
[serve]
# The file whose contents every JSON request to a non-loopback bind must
# present as `Authorization: Bearer ...`. Required when `--serve` names a
# host other than loopback; ignored when it binds loopback.
token_file = "~/.holophyte/holophyte/serve.token"
# A second file whose contents are accepted wherever `token_file`'s are:
# one token for every daemon on this machine. Optional.
machine_token_file = "~/.holophyte/machine.token"
# Answer `POST /actions/restart-supervisor`, `/actions/launch-loop` and
# `/actions/requeue` behind the token, on every bind (so `token_file` is
# required with this on). Off, every `/actions/` path is 404.
actions = false
# Answer `GET /config` (this file, token and key values redacted) and
# `PUT /config` (a replacement, validated as startup validates, written
# beside a `config.toml.bak-STAMP`) behind the token, on every bind. Off
# by default: whoever can write this file writes `[worktree] setup` and
# `[agents]`, which the next loop start runs as commands on this host.
config_edit = false
# The systemd instance those actions address: `holophyte-supervise@NAME`,
# `holophyte-loop@NAME`. The target directory's name when absent.
name = "holophyte"
```

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

`machine_token_file` names a second token file, read wherever `token_file`
is read and held to the same rules; a missing, empty or group- or
world-readable file is the same startup error, naming `[serve]
machine_token_file`. Every route that demands the project's token accepts
this one too, each compared in constant time, so the daemons on one
machine can share a single token that is rotated in one place and kept in
one copy on the operator's machine, while `token_file` stays the token to
hand out for one project alone. It does not stand in for `token_file`:
the binds and routes that need that key still need it. Absent, the daemon
accepts the project's token alone, as before.

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
name when absent. `config_edit` opens this file itself to the console:
`GET /config` is its text with the value of every key named `...token` or
`...key` replaced by `[redacted]`, wherever and however the key is written
(`token_file`, a path, stays; every value under a table so named is
replaced too), and `PUT
/config` is a replacement the daemon holds to the same checks startup
runs -- a refused document is 400 naming the key and nothing is written --
then writes beside a timestamped backup and records as a `config_edit`
intervention; a `[redacted]` sent back is the current value, so a round
trip never blanks a secret. A write that changes `[agents] implementer`
runs the startup probe on it (`[agents]` above) and reports the verdict as
`probe` beside the write, which lands regardless. A `PUT /config` may
also carry `{"patch": {"loop.workers": 3, ...}}`, dotted keys the daemon
sets in this file with `tomlkit` -- comments and layout kept -- and holds
to the same checks; `GET /config` carries the redacted text parsed as
`values` beside it, so the console never parses TOML. The change applies at the
next loop start, not to a running loop. It needs `token_file` on every bind as `actions` does,
and is off by default because the file is command execution on the writer
host (`[worktree] setup`, `[agents]`). All three are read once at bind. The
routes, their bodies and replies are in [The daemon's
actions](reference/daemon.md).

## `[console]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `daemons` | Default: `[]` | List of unique `HOST:PORT` strings with non-empty host and decimal port; set to show other daemons' projects in the console. |

```toml
[console]
# The other daemons the console fans out to, as HOST:PORT strings. Optional;
# absent, the page shows this daemon's project alone.
daemons = ["writer-2:7710", "writer-3:7710"]
```

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

## `[report]`

| Key | Default | Allowed values and when to change |
| --- | --- | --- |
| `host_label` | Default: Absent (hostname) | Non-empty string; set a stable writer role label for reports and board leases. |
| `findings` | Default: `"none"` | `"none"` or `"repo"`; choose repo to render and commit a bounded FINDINGS.md window. |

```toml
[report]
# What the factory prints where it would print the machine's hostname.
# Optional; absent, the hostname is printed as recorded.
host_label = "writer-1"
# Whether the loop renders FINDINGS.md into the target: `none` renders and
# commits nothing, `repo` renders and commits the bounded window at every
# close-out. Optional; the default is `none`.
findings = "none"
```

The store is the run record, read through the console or `--report`;
`FINDINGS.md` is a second copy of it that can drift, so by default
(`findings = "none"`) no close-out writes or commits the file, the merge
path makes no findings commit, and a pull request target's checkout gains
no untracked file. `findings = "repo"` is for a target that wants the
evidence beside its code: the bounded window is rendered and committed at
every close-out, one commit per merge. Any other value fails startup naming
`[report] findings`. Switching a target from `repo` to `none` leaves the
`FINDINGS.md` already in its repository exactly as it is, not deleted; the
operator removes it by hand. `--report` prints the mode in effect below the
table.

`host_label` also names this writer's board lease: the claim labels the
Linear issue `holo:` plus the label (`holo:writer-1` above) for as long as
the run holds the ticket, and another writer skips a ready issue carrying a
`holo:` label that is not its own ([the loop](loop.md), step 1). Two writer
hosts sharing one board therefore need two distinct labels; a host with no
`host_label` leases under its hostname.

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

## `[questions]`

Typed triage of bare PR mentions.

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | Default: `https://api.typesafe.ai/v1/systemone` | Typed-question HTTP(S) endpoint. |
| `key_env` | Default: `TYPESAFE_API_KEY` | Environment variable holding the bearer key. |
| `min_confidence` | Default: `0.6` | Confidence floor, a number from 0 to 1. |

The key is read from that environment variable for each request. Missing keys,
service failures, unclear answers and answers below the floor use the read-only
answer path. Only a confident `fix` requests implementation; explicit `ask:`
and `fix:` markers bypass triage.

Run `python3 scripts/eval_triage.py --config PATH --min-accuracy 0.8` to replay
the labelled fixture against the real service. It prints counts, accuracy and
misses, and exits nonzero below the floor. Unit tests replace the service.
