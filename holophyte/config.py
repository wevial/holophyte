"""Per-target configuration: `config.toml` and the knobs it can set.

The reader (`load_config`) and every table the factory reads -- `[agents]`,
`[worktree]`, `[supervisor]`, `[loop]` -- with the defaults an absent table
leaves in place, the constraints a present value is held to, and the startup
checks that refuse a bad one before anything is claimed. Nothing here knows
the loop, gates or store; readers take a `Project`-shaped value.

"""
import collections
import math
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import review_runner
from holophyte import harness
from holophyte.config_tables import (
    AGENT_FALLBACK_KEYS,
    BOARD_KEYS,
    CONSOLE_KEYS,
    LOOP_KEYS,
    MERGE_KEYS,
    REPORT_KEYS,
    SUPERVISOR_KEYS,
    board_config,
    loop_config,
    merge_config,
    report_config,
    split_address,
    sweep_config,
    verify_config,
)


def load_config(path):
    """Parse the target's TOML config, or `{}` when there is no file.

    Malformed TOML is a startup error; unknown tables remain forward compatible.
    Unknown keys in known tables are refused by `check_config_keys()`.
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
# Claude Code / Opus High implements; the local container boundary runs Codex
# at `[agents] review_model` / `review_effort` (GPT-5.6 Sol Medium when the
# keys are absent) against a detached, zero-remote, read-only candidate.
# These are the defaults an absent `[agents]` table leaves in place, not
# assumptions: a target that names its own command for a role gets that one.
IMPL_MODEL = "opus"
IMPL_EFFORT = "high"
IMPL_TIMEOUT = 1800  # hard wall-clock cap on one implementer turn, seconds
REVIEW_MODEL = review_runner.MODEL
REVIEW_EFFORT = review_runner.EFFORT
REVIEW_EFFORTS = review_runner.EFFORTS
review_profile = review_runner.profile_for
REVIEW_PROFILE = review_profile(REVIEW_MODEL, REVIEW_EFFORT)

# The loop's internal role names, and the `[agents]` key each one reads. The
# config speaks the job title an operator writes on a ticket; the loop speaks
# the verb it dispatches.
AGENT_CONFIG_KEYS = {
    "implement": "implementer",
    "review": "reviewer",
    "adjudicate": "adjudicator",
    "write": "writer",
}

# The `[agents]` keys that choose the Codex route the review container runs,
# for the reviewer and the adjudicator alike -- they share the container, so
# they share the pair. Read by `review_route()`.
REVIEW_ROUTE_KEYS = ("review_model", "review_effort")

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
    "verify": frozenset({"always", "before_merge", "timeout_sec"}),
    "agents": frozenset(AGENT_CONFIG_KEYS.values()) | frozenset(REVIEW_ROUTE_KEYS)
              | frozenset(AGENT_FALLBACK_KEYS) | frozenset({"budget_scale",
                  "implementer_isolation", "implementer_image",
                  "implementer_credential", "implementer_session",
                  "implementer_resume"}),
    "worktree": frozenset({"setup", "setup_timeout_sec", "branch_prefix",
                           "carry", "env_source", "env_allow"}),
}
# `[loop]`'s and `[report]`'s entries are filled in beside `LOOP_KEYS` and
# `REPORT_KEYS`, with `[supervisor]`'s.
KNOWN_KEYS["supervisor"] = frozenset(SUPERVISOR_KEYS)
KNOWN_KEYS["loop"] = frozenset(LOOP_KEYS)
KNOWN_KEYS["board"] = frozenset(BOARD_KEYS)
# The capture allow-list stays out of `MERGE_KEYS`, which `merge_config()`
# checks through `MERGE_VALUES`; `capture_environment()` checks it instead.
KNOWN_KEYS["merge"] = frozenset(MERGE_KEYS) | frozenset(
    {"capture_env_source", "capture_env_allow"})
KNOWN_KEYS["report"] = frozenset(REPORT_KEYS)
KNOWN_KEYS["console"] = frozenset(CONSOLE_KEYS)
KNOWN_KEYS["questions"] = frozenset(("url", "key_env", "min_confidence"))
# `[harnesses]` maps a registered harness to an absolute binary path.
KNOWN_KEYS["harnesses"] = frozenset(harness.ADAPTERS)


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


def check_config(target):
    """Validate config before claiming work, without touching the host.
    CLI startup and daemon writes share these checks; refusals name the setting.
    """
    merge = merge_config(target)
    from holophyte.questions import settings
    try:
        settings(target.config())
    except ValueError as error:
        raise SystemExit(str(error)) from None
    verify_config(target)
    check_config_keys(target)
    harness.check_target(target)
    budget_scale(target)
    implementer_session(target)
    from holophyte.fix_session import resume_template
    resume_template(target)
    from holophyte.isolation import route_for
    route = route_for(target)
    if merge.ui_capture and route.backend == "container" and not route.writable:
        raise SystemExit(
            "[merge] ui_capture requires [agents] implementer_isolation "
            "writable = true for its worktree output directory")
    check_agent_fallbacks(target)
    sweep_config(target)
    loop_config(target)
    report_config(target)
    console_config(target)
    serve_config(target)


def implementer_session(target):
    """Optional compiled session-id pattern; refuse invalid settings at startup."""
    value = config_table(target, "agents").get("implementer_session")
    if value is None:
        return None
    key = f"[holo2] {target.config_path}: [agents] implementer_session"
    if not isinstance(value, str):
        raise SystemExit(f"{key} must be a regular expression string")
    try:
        pattern = re.compile(value)
    except re.error as exc:
        raise SystemExit(f"{key} invalid regular expression: {exc}") from exc
    if pattern.groups != 1:
        raise SystemExit(f"{key} must have exactly one capture group")
    return pattern


def check_document(target):
    """`check_config()` plus the shape of the tables the loop's startup
    reads before it claims: `[board]` through `board_config()`, `[agents]`
    through `agent_command()` and `review_route()`, `[worktree]` through
    `check_worktree_setup()`, the loop's own startup call. What it
    deliberately leaves out is the host: whether a program is on PATH or
    Docker answers (`check_agent_commands()`) is the loop's question at its
    next start, not a property of the document. A relative program path
    is: `check_agent_commands()` refuses it whatever the host holds, so it
    is refused here through the same `check_command_path()`."""
    check_config(target)
    board_config(target)
    review_route(target)
    for role, key in AGENT_CONFIG_KEYS.items():
        argv = agent_command(target, role, "")
        if argv is not None:
            check_command_path(target, key, argv[0])
    check_worktree_setup(target)


def agent_command(target, role, goal, *, fallback=False):
    """The configured argv for `role`, or None when the config names none.

    The goal is appended as the command's last argument, which is where both
    default harnesses take a prompt (`claude ... -p PROMPT`, `codex exec ...
    PROMPT`). Writing it as an argv element rather than interpolating it into
    a shell string is the same rule `sh()` follows: task text is data, and it
    never gets to break quoting.

    A table (`[agents.implementer] harness = "claude"`) is built here too,
    by its `harness` adapter, under a fresh session id
    `harness.agent_session()` reads back, so every caller gets the adapter's
    argv without learning about tables.

    A key that is present but unusable — a non-string, or a string that splits
    to nothing — is a startup error rather than a fallback to the default: the
    operator asked for a route, and quietly running the built-in one instead
    would answer a different question than the one the config asked.
    """
    key = AGENT_CONFIG_KEYS[role] + ("_fallback" if fallback else "")
    command = config_table(target, "agents").get(key)
    if command is None:
        return None
    if isinstance(command, dict):
        return harness.seat(target, role, fallback=fallback).turn(goal)
    if not isinstance(command, str):
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {key} must be "
            f"a command string, got {type(command).__name__}")
    try:
        argv = shlex.split(command)
    except ValueError as bad:
        # `shlex` says "No closing quotation"; the key it was in is ours.
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {key}"
            f" cannot be split into a command: {bad}")
    if not argv:
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {key}"
            " is empty")
    return argv + [goal]


def check_agent_fallbacks(target):
    """Fallback commands obey the primary grammar and name a distinct route."""
    for role, key in AGENT_CONFIG_KEYS.items():
        fallback = agent_command(target, role, "", fallback=True)
        if fallback is None:
            continue
        primary = agent_command(target, role, "")
        if fallback == primary:
            raise SystemExit(f"[holo2] {target.config_path}: [agents] "
                             f"{key}_fallback may not equal {key}")
        check_command_path(target, key + "_fallback", fallback[0])
    review_route(target)


def review_route(target):
    """The `(model, effort)` pair the review container runs, per the config.

    `[agents] review_model` and `review_effort` when set, `REVIEW_MODEL` and
    `REVIEW_EFFORT` when not. Model routing is explicit factory policy, so a
    key that is present is held to what the route can run: the model is a
    non-empty string, the effort one of Codex's `REVIEW_EFFORTS`. A value
    outside that is a startup error naming the table and the key, not a
    fallback to the default -- the operator asked for a route, and quietly
    running another would answer a different question than the config asked.

    Either key beside a `reviewer` command is refused as contradictory: the
    override opts the reviewer out of the container, and the pair chooses
    what runs inside it, so one of the two lines is not doing what its author
    believes. (An `adjudicator` override alone leaves the reviewer in the
    container, so the pair still has a job.)
    """
    agents = config_table(target, "agents")
    model_key, effort_key = REVIEW_ROUTE_KEYS
    for key in REVIEW_ROUTE_KEYS:
        if key in agents and any(k in agents for k in ("reviewer",
                                                       *AGENT_FALLBACK_KEYS)):
            raise SystemExit(
                f"[holo2] {target.config_path}: [agents] {key} beside [agents] "
                f"reviewer or fallback command: the command opts out of the container "
                f"the pair routes -- drop one of the two")
    model = agents.get(model_key, REVIEW_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {model_key} must be a "
            f"non-empty Codex model id, got {model!r}")
    effort = agents.get(effort_key, REVIEW_EFFORT)
    if effort not in REVIEW_EFFORTS:
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {effort_key} must be one of "
            f"{', '.join(REVIEW_EFFORTS)}, got {effort!r}")
    return model, effort


# How much the implementer turn's wall-clock budget stretches on this
# target: `[agents] budget_scale` multiplies the ticket's estimate into the
# cap `loop._timed()` arms, the ceiling `agents.agent()` holds it under,
# and the box the sweep and /status count the run against. A harness that
# reads more and edits later spends the same budget at a slower rate; the
# estimate, the ticket and the template's thirty-minute rule are untouched
# -- the scale is a property of the harness, held beside its route.
BUDGET_SCALE = 1.0
BUDGET_SCALE_RANGE = (1.0, 3.0)


def budget_scale(target):
    """The `[agents] budget_scale` multiplier on the implementer's box.

    `BUDGET_SCALE` when the key is absent -- the estimate exactly, as it
    has always been -- else a number in `BUDGET_SCALE_RANGE`: under 1 the
    "scale" would shrink a budget the cap exists to stop, and past 3 the
    cap stops bounding the turn at all. A value outside, or a non-number
    (`true` included: TOML's boolean is not a 1 the operator meant), is a
    startup error naming the key and the range, the same refusal a bad
    `[supervisor]` threshold gets: a multiplier the factory quietly
    clamped would bound turns with a number nobody chose.
    """
    value = config_table(target, "agents").get("budget_scale")
    if value is None:
        return BUDGET_SCALE
    low, high = BUDGET_SCALE_RANGE
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] budget_scale must be a "
            f"number from {low} to {high}, got {value!r}")
    return value


def check_agent_commands(target):
    """Validate route grammar; check executables on the host only for host seats.

    Container implementers are live-probed before claiming. Host routes without
    fallbacks need installed executables; default reviewers need Docker and an
    image. PR merge mode also checks its remote and authentication prerequisites."""
    from holophyte.isolation import route_for
    isolated = route_for(target).backend == "container"
    review_route(target)
    default_container_keys = []
    for role, key in AGENT_CONFIG_KEYS.items():
        argv = agent_command(target, role, "")
        if role == "write" and argv is not None:
            check_command_path(target, key, argv[0])
        if role == "write" or (role == "implement" and isolated):
            continue
        if argv is None:
            if agent_command(target, role, "", fallback=True) is not None:
                # The live startup probe settles the default route and can
                # activate the configured fallback if its CLI is unavailable.
                continue
            if role == "implement":
                check_default_implementer(target)
            else:
                default_container_keys.append(key)
            continue
        program = argv[0]
        check_command_path(target, key, program)
        if (shutil.which(program) is None
                and agent_command(target, role, "", fallback=True) is None):
            raise SystemExit(
                f"[holo2] {target.config_path}: [agents] {key}: no executable "
                f"{program!r} on PATH")
    if default_container_keys:
        check_default_reviewer(target, default_container_keys)
    # And the merge route, when it leaves the machine: `[merge] mode = "pr"`
    # pushes to `origin` and opens a pull request, so a target with no
    # `origin`, or a host with neither an authenticated `gh` nor a token, is
    # found here rather than by the first approved run reaching the gate with
    # its lease held. Imported at the call: `holophyte.pr` imports the gates,
    # which import this module.
    if merge_config(target).mode == "pr":
        from holophyte.pr import check_pr_route
        check_pr_route(target)


def check_command_path(target, key, program):
    """Refuse a relative program path with a directory in it (`./worker`)
    for `[agents] key`: rounds run with `cwd` set to a task worktree that
    does not exist yet, so the name resolves somewhere no check can look.
    A document constraint, not a host one: `check_document()` applies it
    to a `PUT /config` candidate as `check_agent_commands()` does at
    startup."""
    if os.path.dirname(program) and not os.path.isabs(program):
        raise SystemExit(
            f"[holo2] {target.config_path}: [agents] {key}: relative command path "
            f"{program!r} -- rounds run in a task worktree, so name the "
            f"program by an absolute path or leave it to PATH")


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
    commands = config_table(target, "worktree").get("setup")
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


def config_table(target, name):
    """The target's `[name]` table, `{}` when absent -- refused, naming
    the table, when the key holds anything but a table. The readers of
    `[agents]` and `[worktree]` take their keys through this rather than
    `.get()` on whatever the file holds, so `worktree = "invalid"` is one
    sentence at startup, and the same sentence from `PUT /config`, rather
    than a traceback from the first reader to ask it for a key."""
    table = target.config().get(name)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {target.config_path}: [{name}] must be a table, got "
            f"{type(table).__name__}")
    return table


def check_worktree_setup(target):
    """Parse the `[worktree]` table before the loop claims work.

    `check_agent_commands()`'s sibling, here for the same reason: a table read
    for the first time inside a run would abandon a claimed ticket, a cut
    branch and a held ticket lease over something startup could have said in
    one sentence. It parses through `setup_commands()`, so a table this
    accepts is exactly a table a run would accept.

    What it deliberately does not settle is the commands themselves. They are
    shell, not argv -- `run_verify()` runs them the way it runs a ticket's
    verify command -- and they are written against a worktree that does not
    exist yet, so there is nothing here to resolve them against. Startup
    settles the shape of the table; the worktree settles the rest. The cap
    the commands run under, the branch prefix and the carry list are
    checked here too, for the same reason, and so is `[merge]`'s capture
    allow-list, which shares the `[worktree]` reader. `check_document()` runs
    the same call over a `PUT /config` candidate, so the two cannot drift.
    """
    setup_commands(target)
    setup_timeout(target)
    branch_prefix(target)
    carry_directories(target)
    worktree_environment(target)
    capture_environment(target)


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_environment(text, label="env_source"):
    """Read dotenv assignments without evaluating or unquoting their values."""
    values = {}
    for number, line in enumerate(text.split("\n"), 1):
        line = line.removesuffix("\r").lstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator or not ENV_NAME.fullmatch(name):
            raise ValueError(f"{label}: invalid assignment on line {number}")
        values[name] = value
    return values


def worktree_environment(target):
    """Validate and read the allow-list; None preserves targets without it.

    Relative sources resolve beside config.toml. All source values are held
    only in memory for redaction, including values excluded from the checkout.
    """
    return allowed_environment(target, "worktree", "env_source", "env_allow")


def capture_environment(target):
    """`[merge]`'s capture-only allow-list, read like `[worktree]`'s.

    Only `pr_media._capture()` adds these values to a command's environment:
    they are never written to the worktree and never reach agent turns,
    verify commands or `isolation.environment()`.
    """
    return allowed_environment(target, "merge", "capture_env_source",
                               "capture_env_allow")


def allowed_environment(target, name, source_key, allow_key):
    """Both-or-neither `source_key`/`allow_key` in table `name`, read and
    checked; None when neither is set. Every source value is registered for
    redaction, and a refusal names keys and variables, never a value."""
    from holophyte.redact import register_values

    table = config_table(target, name)
    if source_key not in table and allow_key not in table:
        return None
    prefix = f"[holo2] {target.config_path}: [{name}] "
    for key in (source_key, allow_key):
        if key not in table:
            raise SystemExit(prefix + f"missing {key}; "
                             "both environment keys are required")
    source, allow = table[source_key], table[allow_key]
    if not isinstance(source, str) or not source.strip():
        raise SystemExit(prefix + f"{source_key} must be a non-empty path")
    if not isinstance(allow, list) or any(
            not isinstance(item, str) or not ENV_NAME.fullmatch(item)
            for item in allow):
        raise SystemExit(prefix + f"{allow_key} must be a list of variable names "
                         "matching [A-Za-z_][A-Za-z0-9_]*")
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path(target.config_path).parent / path
    try:
        values = parse_environment(path.read_text(encoding="utf-8"), source_key)
    except (OSError, UnicodeError):
        raise SystemExit(prefix + f"{source_key} could not be read as UTF-8") from None
    except ValueError as error:
        raise SystemExit(prefix + str(error)) from None
    register_values(values.values())
    missing = [item for item in allow if item not in values]
    if missing:
        raise SystemExit(prefix + f"{source_key} lacks: " + ", ".join(missing))
    return {item: values[item] for item in allow}


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
    value = config_table(target, "worktree").get("setup_timeout_sec")
    if value is None:
        return VERIFY_TIMEOUT
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise SystemExit(
            f"[holo2] {target.config_path}: [worktree] setup_timeout_sec must be a "
            f"finite positive number of seconds, got {value!r}")
    return value


def carry_directories(target):
    """The target's `[worktree] carry` list, or `[]` when it names none.

    Each entry is a repository-relative directory `[worktree] setup` installs
    and git ignores -- `console/node_modules`, `.venv` -- that the review
    stage copies in read-only so the reviewer can run the ticket's verify
    commands (`review_runner.stage_candidate()`). Startup settles the shape:
    a list of non-empty relative paths with no `..` segment. Whether an
    entry exists, is ignored and is untracked is the stage's question, asked
    against the worktree the round is about, and answered there with a
    boundary error naming the entry.
    """
    entries = config_table(target, "worktree").get("carry")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise SystemExit(
            f"[holo2] {target.config_path}: [worktree] carry must be a list of "
            f"repository-relative directories, got {type(entries).__name__}")
    for entry in entries:
        if not isinstance(entry, str):
            raise SystemExit(
                f"[holo2] {target.config_path}: [worktree] carry: every entry must "
                f"be a repository-relative directory, got {type(entry).__name__}")
        parts = pathlib.PurePosixPath(entry).parts
        if (not entry.strip() or entry.startswith("/") or not parts
                or ".." in parts):
            raise SystemExit(
                f"[holo2] {target.config_path}: [worktree] carry: entry {entry!r} "
                "must be a relative path inside the repository")
    return entries


# Characters git refuses anywhere in a ref name (`git check-ref-format`),
# plus the slash the prefix must not carry: the branch is exactly
# `PREFIX/IDENT-SLUG`, one segment before the identifier.
BRANCH_PREFIX_REFUSED = set("/~^:?*[\\")
DEFAULT_BRANCH_PREFIX = "task"


def branch_prefix(target):
    """The segment ahead of the slash in a task branch name.

    `[worktree] branch_prefix` when the target names one, else `task` -- so a
    repository with its own convention (`factory/`, `ko/`) keeps it, and one
    without keeps today's names byte for byte. Everything after the slash is
    the ticket identifier and title slug, unchanged: the identifier is what
    makes a preserved branch traceable, whatever it sits behind.

    The value is held to what `git check-ref-format --branch` would accept
    as a single segment -- non-empty, no whitespace, no slash, none of
    `~^:?*[` and backslash, no leading `.` or `-` (git would read the latter
    as an option), no trailing `.`, no `..`, `@{` or `.lock` -- and a value
    outside it is a startup error naming the key, for the reason a bad
    `setup_timeout_sec` is: a prefix the factory only discovered was illegal
    at `git worktree add` would abandon a claimed ticket over something one
    sentence at startup could have said.
    """
    value = config_table(target, "worktree").get("branch_prefix")
    if value is None:
        return DEFAULT_BRANCH_PREFIX
    if not isinstance(value, str):
        raise SystemExit(
            f"[holo2] {target.config_path}: [worktree] branch_prefix must be a "
            f"string, got {type(value).__name__}")
    if not value:
        raise SystemExit(
            f"[holo2] {target.config_path}: [worktree] branch_prefix must not be "
            "empty")
    if (any(c.isspace() or c in BRANCH_PREFIX_REFUSED or ord(c) < 0x20 or c == "\x7f"
            for c in value)
            or value.startswith((".", "-")) or value.endswith((".", ".lock"))
            or ".." in value or "@{" in value or value == "@"):
        raise SystemExit(
            f"[holo2] {target.config_path}: [worktree] branch_prefix {value!r} is "
            "not a legal branch segment: no whitespace, no '/', none of "
            "~ ^ : ? * [ \\, no leading '.' or '-', no '..', and not ending in "
            "'.' or '.lock'")
    return value


ConsoleConfig = collections.namedtuple("ConsoleConfig", ("daemons",))


def console_config(target):
    """The target's `[console]` knobs over the defaults.

    Checked at startup beside `report_config()`, the same way: an absent
    table (or key) is the defaults exactly -- no other daemons -- and a
    present `daemons` has to be a list of `HOST:PORT` strings, each one
    `split_address()` accepts and none of them twice: `"nope"` names no
    port to fetch, `""` nothing at all, and a duplicate would draw one
    host's projects twice. The refusal names the table, the key and the
    entry, like a bad `[report]` value. Keys this version does not know
    are refused by `check_config_keys()`.
    """
    table = target.config().get("console", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {target.config_path}: [console] must be a table, got "
            f"{type(table).__name__}")
    daemons = table.get("daemons", CONSOLE_KEYS["daemons"])
    if not isinstance(daemons, (list, tuple)):
        raise SystemExit(
            f"[holo2] {target.config_path}: [console] daemons must be a list "
            f"of HOST:PORT strings, got {daemons!r}")
    seen = set()
    for entry in daemons:
        try:
            if not isinstance(entry, str):
                raise ValueError(f"expected HOST:PORT, got {entry!r}")
            split_address(entry)
        except ValueError as error:
            raise SystemExit(
                f"[holo2] {target.config_path}: [console] daemons: {error}"
            ) from None
        if entry in seen:
            raise SystemExit(
                f"[holo2] {target.config_path}: [console] daemons: {entry!r} "
                f"is listed twice")
        seen.add(entry)
    return ConsoleConfig(daemons=tuple(daemons))


# The daemon's bearer token. `--serve`'s bind address is its only boundary,
# and once the bind is anything but loopback that is not enough: `[serve]
# token_file` names a file whose contents every JSON request must present
# as `Authorization: Bearer ...`. A non-loopback bind without it is a
# startup error; a loopback bind ignores it. The file, not the token, lives
# in config, so the config can be committed to a host's notes and the
# token cannot. `holophyte.serve` reads the file and holds it to a private
# mode. `machine_token_file` (KO-647) names a second file, one token for
# every daemon on the machine, accepted wherever the project's token is;
# `token_file` stays for sharing one project without the machine.
# `actions` opts the daemon into the three `POST /actions/...` routes
# (KO-348): restart the supervisor unit, start the loop unit, requeue a
# ticket. Off, every `/actions/` path is 404 and the daemon writes nothing.
# `name` is the systemd instance those routes address --
# `holophyte-supervise@NAME`, `holophyte-loop@NAME` -- the target slug the
# deploy units are enabled under; the target directory's name by default.
# `config_edit` (KO-356) opens `GET /config` and `PUT /config` behind the
# token: the file's text, secrets redacted, and a validated replacement.
# Off by default and separate from `actions`: a client holding the bearer
# that can write the file can write `[worktree] setup` and `[agents]`,
# which is command execution on the writer host at the next loop start.
SERVE_KEYS = {
    "token_file": None,
    "machine_token_file": None,
    "actions": False,
    "config_edit": False,
    "transcripts": [],
    "name": None,
}
KNOWN_KEYS["serve"] = frozenset(SERVE_KEYS)
ServeConfig = collections.namedtuple(
    "ServeConfig", ("token_file", "machine_token_file", "actions", "name",
                    "config_edit", "transcripts"))


def serve_config(target):
    """The target's `[serve]` knobs over the defaults.

    An absent table (or key) is no token file; a present `token_file` or
    `machine_token_file` must be a non-empty string, the path as written --
    `~` is expanded, a relative path is taken against the config's
    directory, so the file sits beside the config it is named in. Whether
    the daemon needs it at all is `holophyte.serve`'s to decide from the
    bind address; this only holds the value to its shape. `actions` is a
    boolean, false by default, as is `config_edit`, which opens the
    `/config` routes (KO-356); `name` is the systemd instance name the
    action routes address, the target directory's name when absent
    (KO-348). Keys this version does not know are refused by
    `check_config_keys()`.
    """
    table = target.config().get("serve", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {target.config_path}: [serve] must be a table, got "
            f"{type(table).__name__}")
    flags = {}
    for key in ("actions", "config_edit"):
        flags[key] = table.get(key, SERVE_KEYS[key])
        if not isinstance(flags[key], bool):
            raise SystemExit(
                f"[holo2] {target.config_path}: [serve] {key} must be true or "
                f"false, got {flags[key]!r}")
    actions, config_edit = flags["actions"], flags["config_edit"]
    name = table.get("name", SERVE_KEYS["name"])
    if name is None:
        name = target.path.name
    elif not isinstance(name, str) or not name.strip() or "/" in name:
        raise SystemExit(
            f"[holo2] {target.config_path}: [serve] name must be a non-empty "
            f"systemd instance name without '/', got {name!r}")
    from holophyte.transcript_config import transcript_roots
    transcripts = transcript_roots(target, table.get("transcripts", []))
    return ServeConfig(
        token_file=token_path(target, table, "token_file"),
        machine_token_file=token_path(target, table, "machine_token_file"),
        actions=actions, name=name, config_edit=config_edit,
        transcripts=transcripts)


def token_path(target, table, key):
    """`[serve] KEY` as a path, or None when absent: a non-empty string,
    `~` expanded, a relative path taken against the config's directory."""
    value = table.get(key, SERVE_KEYS[key])
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(
            f"[holo2] {target.config_path}: [serve] {key} must be a "
            f"non-empty path, got {value!r}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(target.config_path).parent / path
    return path
