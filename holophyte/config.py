"""Per-target configuration: `config.toml` and the knobs it can set.

The reader (`load_config`) and every table the factory reads -- `[agents]`,
`[worktree]`, `[supervisor]`, `[loop]` -- with the defaults an absent table
leaves in place, the constraints a present value is held to, and the startup
checks that refuse a bad one before anything is claimed. Nothing here knows
the loop, the gates or the store: a function takes a `Target`-shaped value
(`.config()`, `.config_path`) and answers about its config.

First slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import collections
import math
import os
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import review_runner


def load_config(path):
    """Parse the target's TOML config, or `{}` when there is no file.

    An absent file is the common case and means "all defaults" — the factory
    ships no config of its own. A file that exists but does not parse is a
    startup error naming the file and what `tomllib` objected to: a config the
    operator wrote and the factory silently ignored would route a run to a
    harness nobody chose, which is the one outcome the file exists to prevent.
    Unknown tables are left alone, so a config written for a later version
    still loads here; a key this version does not read inside a table it does
    is refused by `check_config_keys()` at startup.
    """
    path = Path(path)
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"[holo2] malformed config {path}: {exc}") from exc


VERIFY_TIMEOUT = 300  # per-command wall-clock cap, verify and worktree setup


# Role -> harness/model pins. Each gate uses a distinct, live-probed route:
# Claude Code / Opus High implements; the local container boundary runs Codex /
# GPT-5.6 Sol Medium against a detached, zero-remote, read-only candidate.
# These are the defaults an absent `[agents]` table leaves in place, not
# assumptions: a target that names its own command for a role gets that one.
IMPL_MODEL = "opus"
IMPL_EFFORT = "high"
IMPL_TIMEOUT = 1800  # hard wall-clock cap on one implementer turn, seconds
REVIEW_PROFILE = "codex-sol-medium"

# The loop's internal role names, and the `[agents]` key each one reads. The
# config speaks the job title an operator writes on a ticket; the loop speaks
# the verb it dispatches.
AGENT_CONFIG_KEYS = {
    "implement": "implementer",
    "review": "reviewer",
    "adjudicate": "adjudicator",
}

# The programs the default routes stand on, and how long the startup probe
# waits for the Docker daemon to answer. A daemon that takes longer than this
# to say hello is not one a review round is going to get anywhere with.
DEFAULT_IMPLEMENTER = "claude"
DEFAULT_REVIEWER = "docker"
DOCKER_PROBE_TIMEOUT = 5

# Every key the factory reads, per table it reads. `check_config_keys()` holds
# a config to this at startup: a key inside one of these tables that is not
# listed here is a typo (`setup_timeout_min` for `setup_timeout_sec`), and a
# typo the factory ignored would leave the operator believing a knob is set
# that is not. Tables not named here are left alone -- a config written for a
# later version, or for another tool reading the same file, still loads.
# `[supervisor]`'s entry is filled in beside `SUPERVISOR_KEYS`, where those
# knobs and their defaults are defined.
KNOWN_KEYS = {
    "agents": frozenset(AGENT_CONFIG_KEYS.values()),
    "worktree": frozenset({"setup", "setup_timeout_sec"}),
}
# `[loop]`'s and `[report]`'s entries are filled in beside `LOOP_KEYS` and
# `REPORT_KEYS`, with `[supervisor]`'s.


def check_config_keys(target):
    """Refuse a key the factory does not read inside a table it does.

    Runs at startup for every mode, in the same breath as `sweep_config()`
    checks the `[supervisor]` values: an unknown key is the same kind of
    mistake as a value outside its constraint, and deserves the same loud
    answer while nothing is claimed. The message names the file, the table,
    the key and the keys the table does accept, so the operator can see the
    one they meant. A table that is not a table is left to the reader that
    owns it (`agent_command()`, `setup_commands()`, `sweep_config()`), which
    already says so in its own words.
    """
    for table, known in KNOWN_KEYS.items():
        section = target.config().get(table)
        if not isinstance(section, dict):
            continue
        for key in section:
            if key not in known:
                raise SystemExit(
                    f"[holo2] {target.config_path}: [{table}] {key}: unknown key; "
                    f"[{table}] accepts: {', '.join(sorted(known))}")


def agent_command(target, role, goal):
    """The configured argv for `role`, or None when the config names none.

    The goal is appended as the command's last argument, which is where both
    default harnesses take a prompt (`claude ... -p PROMPT`, `codex exec ...
    PROMPT`). Writing it as an argv element rather than interpolating it into
    a shell string is the same rule `sh()` follows: task text is data, and it
    never gets to break quoting.

    A key that is present but unusable — a non-string, or a string that splits
    to nothing — is a startup error rather than a fallback to the default: the
    operator asked for a route, and quietly running the built-in one instead
    would answer a different question than the one the config asked.
    """
    command = (target.config().get("agents") or {}).get(AGENT_CONFIG_KEYS[role])
    if command is None:
        return None
    if not isinstance(command, str):
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {AGENT_CONFIG_KEYS[role]} must be "
            f"a command string, got {type(command).__name__}")
    argv = shlex.split(command)
    if not argv:
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {AGENT_CONFIG_KEYS[role]}"
            " is empty")
    return argv + [goal]


def check_agent_commands(target):
    """Resolve every configured `[agents]` command before the loop claims work.

    Reading the config at startup only proved the file was TOML. The commands
    it named were first looked at when a round dispatched them, which is after
    a ticket is claimed, its branch cut and its worktree created: a typo in a
    program name or a stray quote in `reviewer` surfaced as a mid-run
    `FileNotFoundError`, with a run already in flight and its lease held. The
    same mistakes are caught here, before anything is claimed, where the only
    cost of being wrong is an error message.

    The check parses through `agent_command()` rather than re-reading the
    table, so a string this refuses is exactly a string a round would have
    refused, and one it accepts splits at startup into the argv the round will
    dispatch -- no second, kinder parser to disagree with the real one.

    What it can settle here is the program: it has to resolve, on this PATH,
    to a file that is executable. What it deliberately does not do is run it.
    A configured route is an agent turn; probing it live would dispatch a real
    one, against no ticket, on every startup.

    A relative program path with a directory in it (`./review.sh`) is refused
    rather than guessed at. Rounds run with `cwd` set to a task worktree that
    does not exist yet, so that name resolves somewhere this check cannot look
    and the operator has not named. An absolute path or a PATH lookup says
    where it means.

    A role the table does not name takes its default route, and that route is
    held to the same bar as a configured one: the default implementer is
    `claude` on PATH, and the default reviewer and adjudicator run inside a
    container, so `docker` has to be on PATH and its daemon has to answer. A
    host with Docker stopped used to claim a ticket, cut a branch and fail at
    the first review with the lease held. `docker info` is a liveness probe of
    the daemon, not an agent turn -- no review is staged and the image is
    neither pulled nor built, since the runner builds it on first use; the
    image is only looked up, so a host that has yet to build it hears so.
    """
    default_container_keys = []
    for role, key in AGENT_CONFIG_KEYS.items():
        argv = agent_command(target, role, "")
        if argv is None:
            if role == "implement":
                check_default_implementer(target)
            else:
                default_container_keys.append(key)
            continue
        program = argv[0]
        if os.path.dirname(program) and not os.path.isabs(program):
            raise SystemExit(
                f"[holo2] {target.config_path}: [agents] {key}: relative command path "
                f"{program!r} -- rounds run in a task worktree, so name the "
                f"program by an absolute path or leave it to PATH")
        if shutil.which(program) is None:
            raise SystemExit(
                f"[holo2] {target.config_path}: [agents] {key}: no executable "
                f"{program!r} on PATH")
    if default_container_keys:
        check_default_reviewer(target, default_container_keys)


def check_default_implementer(target):
    """The default implementer route is `claude` on PATH; nothing else."""
    if shutil.which(DEFAULT_IMPLEMENTER) is None:
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] implementer is not set, so the "
            f"implementer runs `{DEFAULT_IMPLEMENTER}`, and there is no "
            f"executable {DEFAULT_IMPLEMENTER!r} on PATH -- install the Claude "
            f"CLI or set [agents] implementer to the command to run instead")


def check_default_reviewer(target, keys):
    """The default container route needs `docker` and a daemon that answers.

    `keys` are the `[agents]` keys whose roles fall to that route, named in
    the message so the operator knows which line to write to route around it.
    The daemon is asked `docker info` under `DOCKER_PROBE_TIMEOUT`: a daemon
    that is stopped answers at once with a connection error, and one that is
    wedged does not answer at all, and both are the same startup error.

    With the daemon up, the review image is looked up too, and its state is
    reported rather than enforced: `review_runner` builds the image on the
    first review that finds it missing, so an unbuilt image is what a fresh
    host looks like, not a route that is broken. What the operator learns is
    that the first review round will spend its time on a build, and where the
    Dockerfile it builds from lives.
    """
    unset = " and ".join(keys)
    remedy = (f"start the Docker daemon or set [agents] {unset} to the "
              f"command to run instead")
    if shutil.which(DEFAULT_REVIEWER) is None:
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {unset} not set, so the review "
            f"runs in a `{DEFAULT_REVIEWER}` container ({review_runner.IMAGE}), "
            f"and there is no executable {DEFAULT_REVIEWER!r} on PATH -- "
            f"install Docker or set [agents] {unset} to the command to run "
            f"instead")
    probe = docker_probe(target, ["info"], unset, remedy)
    if probe.returncode:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit {probe.returncode}"
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {unset} not set, so the review "
            f"runs in a `{DEFAULT_REVIEWER}` container, and the Docker daemon "
            f"did not answer `{DEFAULT_REVIEWER} info`: {reason} -- {remedy}")
    image = docker_probe(target, ["image", "inspect", review_runner.IMAGE],
                         unset, remedy)
    if image.returncode:
        print(f"[holo2] review image {review_runner.IMAGE} is not built on this "
              f"host; the first review round builds it from "
              f"{review_runner.DOCKERFILE}")


def docker_probe(target, args, unset, remedy):
    """Ask the daemon `docker <args>` under `DOCKER_PROBE_TIMEOUT`.

    A daemon that does not answer in time is a startup error naming the
    probe, whatever it was asking; a daemon that answers, with any exit
    status, hands its result back for the caller to read.
    """
    argv = [DEFAULT_REVIEWER, *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=DOCKER_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {unset} not set, so the review "
            f"runs in a `{DEFAULT_REVIEWER}` container, and the Docker daemon "
            f"did not answer `{' '.join(argv)}` within "
            f"{DOCKER_PROBE_TIMEOUT}s -- {remedy}") from None


# --- worktree setup ----------------------------------------------------------
# The second table a target can write. `[worktree] setup` is the list of shell
# commands a freshly cut task worktree needs before an agent works in it: the
# venv, module download or generated file the target's toolchain would
# otherwise borrow from the main checkout, quietly, and get wrong the moment a
# task changes a dependency. An absent table is today's behavior -- nothing
# runs, and a run costs exactly what it costs now.


def setup_commands(target):
    """The target's `[worktree] setup` list, or `[]` when it names none.

    Each entry is one shell command, run in order. A table that is present but
    unusable -- not a list, an entry that is not a string, an entry that is
    blank -- is an error rather than a skipped step, for the reason
    `agent_command()` refuses a bad `[agents]` row: a setup command the
    operator wrote and the loop silently dropped would hand the implementer a
    worktree nobody prepared, and that surfaces far away from the config, as a
    toolchain failure in the middle of a round.
    """
    commands = (target.config().get("worktree") or {}).get("setup")
    if commands is None:
        return []
    if not isinstance(commands, list):
        raise SystemExit(
            f"[holo2] {target.config_path}: [worktree] setup must be a list of "
            f"command strings, got {type(commands).__name__}")
    for command in commands:
        if not isinstance(command, str):
            raise SystemExit(
                f"[holo2] {target.config_path}: [worktree] setup: every entry must be "
                f"a command string, got {type(command).__name__}")
        if not command.strip():
            raise SystemExit(
                f"[holo2] {target.config_path}: [worktree] setup: entry {command!r} "
                "is empty")
    return commands


def setup_timeout(target):
    """The per-command cap on `[worktree] setup`, in seconds.

    `[worktree] setup_timeout_sec` when the target names one, else the same
    `VERIFY_TIMEOUT` a verify command gets: setup is a build step, and a Go
    module download or a fat pip install legitimately needs more patience
    than stdlib Python's nothing. The value is held to the constraint
    `sweep_config()` holds an interval to -- a finite positive number, with
    booleans refused as numbers -- and a value outside it is a startup error
    naming the key, for the reason a bad `[supervisor]` value is: a cap the
    factory quietly replaced with its default would bound the setup with a
    number nobody chose.
    """
    value = (target.config().get("worktree") or {}).get("setup_timeout_sec")
    if value is None:
        return VERIFY_TIMEOUT
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise SystemExit(
            f"[holo2] {target.config_path}: [worktree] setup_timeout_sec must be a "
            f"finite positive number of seconds, got {value!r}")
    return value


# How old a heartbeat has to be before a sighting counts as silent, and how
# many consecutive silent sightings trip the run. Two, from the v1 TUI mining:
# one sample false-positives on a load spike, and a supervisor that kills live
# runs is worse than one that notices a dead one a minute late.
HEARTBEAT_STALE_MS = 5 * 60 * 1000
STALE_STRIKES = 2
# How far past its claim-time estimate a run may run before the time box is
# considered blown. Generous on purpose: the estimate is a 15-30 minute
# guess, and the trip is meant to catch a run that is not going to finish
# rather than one that is merely slower than the ticket hoped.
BUDGET_GRACE = 1.5
# How much of their findings two consecutive review rounds may share before
# the review is read as circling rather than converging: the Jaccard overlap
# `store.findings_overlap()` measures, over the `(path, line, severity)` keys
# the fingerprint hashes. Half, because a fix round that leaves half of the
# reviewer's complaints standing has not moved the review, and the round after
# it is the terminal adjudication -- a doomed one is cheaper failed now than
# paid for. Two rounds are compared and never one: a healthy run sits in
# `reviewing` with a single round on file, and there is nothing to compare
# it against.
REVIEW_OVERLAP_THRESHOLD = 0.5
# How long the supervisor sleeps between two acting sweeps. A minute: fine
# enough that a dead run is noticed within `HEARTBEAT_STALE_MS` plus one
# interval of dying, coarse enough that the store's write lock is taken for
# the sweep's arithmetic sixty times an hour and not six hundred.
SUPERVISE_INTERVAL_SEC = 60
# How long a self-merge re-exec may take to come back before its silence is
# reported. The loop writes a `loopRestarts` row just before `os.execv()`
# replaces it, and a loop that came back claims a ticket (a heartbeat) or
# writes its exit note; a restart older than this that neither has followed
# is a loop that died in the exec -- the one gap every earlier gate had
# passed through when the first live re-exec after KO-191 died with
# `FileNotFoundError` before printing anything. Two minutes: an exec is
# instant and a startup probe is seconds, so a loop that has not claimed or
# exited in two minutes is not merely slow.
RESTART_GRACE_SEC = 120

# The six knobs above have an address: the optional `[supervisor]` table of
# `<repo>.holophyte.toml`. Different targets legitimately want different
# patience -- a Go build's setup is slower than stdlib Python's -- and the
# constants are the defaults, not the lookup sites: an absent table is
# exactly the numbers above. The keys are named in the units an operator
# thinks in (minutes, seconds, a multiplier, a fraction) and `sweep_config()`
# converts them to the units the sweep computes in.
SUPERVISOR_KEYS = {
    "heartbeat_stale_min": HEARTBEAT_STALE_MS / 60000,
    "stale_strikes": STALE_STRIKES,
    "budget_grace": BUDGET_GRACE,
    "review_overlap_threshold": REVIEW_OVERLAP_THRESHOLD,
    "sweep_interval_sec": SUPERVISE_INTERVAL_SEC,
    "restart_grace_sec": RESTART_GRACE_SEC,
}
# The knobs as the sweep reads them: the same six, with the heartbeat
# threshold and the restart grace already in milliseconds, so the arithmetic
# in `sweep()` is the arithmetic it always was.
KNOWN_KEYS["supervisor"] = frozenset(SUPERVISOR_KEYS)
SweepConfig = collections.namedtuple(
    "SweepConfig",
    ("heartbeat_stale_ms", "stale_strikes", "budget_grace",
     "review_overlap_threshold", "sweep_interval_sec", "restart_grace_ms"))


def sweep_config(target):
    """The target's sweep thresholds: `[supervisor]` over the defaults.

    Every key is optional and an absent table is the module constants exactly.
    A key that is present is checked here, the way `agent_command()` checks a
    route: a threshold is a number, thresholds and intervals are positive,
    the strike requirement is a whole number of sightings, and the overlap is
    a fraction in (0, 1] -- a share of findings above one is unreachable, and
    a share of zero trips every review that found anything at all. A value
    outside its constraint is a startup error naming the key and the
    constraint, like malformed TOML: a negative threshold the factory quietly
    replaced with its default would sweep with numbers nobody chose. Booleans
    are refused as numbers, because `true` is a 1 TOML never meant, and so
    are `inf` and `nan`, which TOML also spells: an infinite threshold is a
    trip that silently never fires, and an infinite interval is a `sleep()`
    that raises OverflowError instead of sleeping.

    Keys the table names that this version does not know are refused by
    `check_config_keys()`, which startup runs beside this.
    """
    table = target.config().get("supervisor", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {target.config_path}: [supervisor] must be a table, got "
            f"{type(table).__name__}")
    values = {}
    for key, default in SUPERVISOR_KEYS.items():
        value = table.get(key, default)
        number = (isinstance(value, (int, float))
                  and not isinstance(value, bool) and math.isfinite(value))
        if key == "stale_strikes":
            constraint, ok = "a positive integer", number and (
                isinstance(value, int) and value > 0)
        elif key == "review_overlap_threshold":
            constraint, ok = "a number in (0, 1]", number and 0 < value <= 1
        else:
            constraint, ok = "a finite positive number", number and value > 0
        if not ok:
            raise SystemExit(
                f"[holo2] {target.config_path}: [supervisor] {key} must be "
                f"{constraint}, got {value!r}")
        values[key] = value
    return SweepConfig(
        heartbeat_stale_ms=int(values["heartbeat_stale_min"] * 60000),
        stale_strikes=values["stale_strikes"],
        budget_grace=values["budget_grace"],
        review_overlap_threshold=values["review_overlap_threshold"],
        sweep_interval_sec=values["sweep_interval_sec"],
        restart_grace_ms=values["restart_grace_sec"] * 1000)

# What the claim loop does after a run it closed out as failed. The default
# is the loop as it has always been: one failure ends the process, and an
# operator relaunches it. `stop_on_failure = false` is for the unattended
# night once escalation is trusted -- the failed run is recorded exactly as
# today, and the loop goes on to the next ready ticket instead of exiting.
# Escalation (`MAX_FAILED_RUNS`) is untouched: a ticket that keeps failing
# still parks itself; this knob only decides whether one failure stops the
# whole queue.
#
# `order` is which ready ticket the loop claims first. `"identifier"` is the
# loop as it has always been: lowest identifier first. `"priority"` claims
# the most urgent Linear priority first (1 before 2 before 3 before 4, then
# unprioritised), identifier ascending within a priority -- the policy for a
# queue with more than one author, where a P1 filed after ten P3s should not
# wait behind all of them. The file board has no priority and orders by
# identifier under either value.
# `spawn_supervisor`: whether the loop starts a detached `--supervise` for
# its target at startup when no live one holds the supervisor lock. On by
# default, so one command runs the factory; `false` for an operator whose
# service manager runs the supervisor as a unit of its own.
LOOP_KEYS = {
    "stop_on_failure": True,
    "order": "identifier",
    "spawn_supervisor": True,
}
LOOP_ORDERS = ("identifier", "priority")
KNOWN_KEYS["loop"] = frozenset(LOOP_KEYS)
LoopConfig = collections.namedtuple(
    "LoopConfig", ("stop_on_failure", "order", "spawn_supervisor"))


def loop_config(target):
    """The target's `[loop]` knobs over the defaults.

    Checked at startup beside `sweep_config()`, the same way: an absent table
    is the defaults exactly, and a present value has to be the type the key
    means. `stop_on_failure` and `spawn_supervisor` are booleans, and only
    booleans -- `"yes"`,
    `1` and `"false"` are all truthy strings or numbers TOML never meant as
    the answer, and a value the factory quietly read as one would run a
    night nobody chose. `order` is one of `LOOP_ORDERS`, and only one of
    those -- `"urgent"` or `1` names no sort the loop has. The refusal names
    the table, the key and the constraint, like a bad `[supervisor]`
    threshold. Keys this version does not know are refused by
    `check_config_keys()`.
    """
    table = target.config().get("loop", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {target.config_path}: [loop] must be a table, got "
            f"{type(table).__name__}")
    values = {}
    for key, default in LOOP_KEYS.items():
        value = table.get(key, default)
        if isinstance(default, bool) and not isinstance(value, bool):
            raise SystemExit(
                f"[holo2] {target.config_path}: [loop] {key} must be a boolean "
                f"(true or false), got {value!r}")
        if key == "order" and value not in LOOP_ORDERS:
            allowed = " or ".join(f'"{o}"' for o in LOOP_ORDERS)
            raise SystemExit(
                f"[holo2] {target.config_path}: [loop] {key} must be one of "
                f"{allowed}, got {value!r}")
        values[key] = value
    return LoopConfig(**values)


# The board the loop claims from: the Linear project's UUID and the name of
# the team it belongs to (the workflow states are looked up per team). Both
# live in the target's config because a target is a repository plus its
# store plus its config, and the board it is driven from belongs with them:
# read from one process-wide variable, two loops on one host for two targets
# claim from the same project, and the second silently works the first's
# queue. Neither has a default -- a board is one operator's, never this
# file's -- so a loop with no table exits at startup naming the key.
BOARD_KEYS = {
    "project_id": None,
    "team": None,
}
KNOWN_KEYS["board"] = frozenset(BOARD_KEYS)
BoardConfig = collections.namedtuple("BoardConfig", ("project_id", "team"))


def board_config(target):
    """The target's `[board]`, or `None` when the table is absent.

    A present table has to carry both keys as non-empty strings: half a
    board names no project to claim from or no team to resolve states in,
    and the refusal names the table, the key and the constraint, like a bad
    `[loop]` value. An absent table is `None`, and the caller decides
    whether its mode needs a board: `--report` and a read-only `--sweep`
    call nobody; the loop exits at startup naming `[board] project_id`.
    Nothing is read from the environment. Keys this version does not know
    are refused by `check_config_keys()`.
    """
    table = target.config().get("board")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {target.config_path}: [board] must be a table, got "
            f"{type(table).__name__}")
    values = {}
    for key in BOARD_KEYS:
        value = table.get(key)
        if not isinstance(value, str) or not value:
            raise SystemExit(
                f"[holo2] {target.config_path}: [board] {key} must be a "
                f"non-empty string, got {value!r}")
        values[key] = value
    return BoardConfig(**values)



# What the factory prints where it would print the writer host's hostname:
# the `host` column of `--report` and `--sweep` and the supervisor's startup
# and refusal lines. (The FINDINGS window the loop commits to a public
# repository renders no host at all: its run and round entries never carried
# one.) The column exists so a reader can tell which writer produced a run
# when there is more than one; a stable label does that job without naming
# a personal machine. The store keeps recording the real hostname
# (`runs.host`, `supervisorHeartbeats.host`, the lock file), which the
# supervisor compares against its own -- and the label stays out of the
# store on purpose, so it can be renamed later without a migration.
REPORT_KEYS = {
    "host_label": None,
}
KNOWN_KEYS["report"] = frozenset(REPORT_KEYS)
ReportConfig = collections.namedtuple("ReportConfig", ("host_label",))


def report_config(target):
    """The target's `[report]` knobs over the defaults.

    Checked at startup beside `loop_config()`, the same way: an absent table
    (or key) is the defaults exactly -- no label, the hostname rendered as
    it always was -- and a present `host_label` has to be a string, and a
    non-empty one: `3` names no writer, and `""` would render every host as
    nothing, which is the invisible blank `host_name()`'s `?` exists to
    avoid. The refusal names the table, the key and the constraint, like a
    bad `[loop]` value. Keys this version does not know are refused by
    `check_config_keys()`.
    """
    table = target.config().get("report", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {target.config_path}: [report] must be a table, got "
            f"{type(table).__name__}")
    values = {}
    for key, default in REPORT_KEYS.items():
        value = table.get(key, default)
        if value is not None and not (isinstance(value, str) and value.strip()):
            raise SystemExit(
                f"[holo2] {target.config_path}: [report] {key} must be a "
                f"non-empty string, got {value!r}")
        values[key] = value
    return ReportConfig(**values)
