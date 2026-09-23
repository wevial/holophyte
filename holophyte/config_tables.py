"""Per-table config keys, defaults and validated readers.

The key sets are registered in config.KNOWN_KEYS; readers take a Target and
refuse invalid values with the table, key and constraint. Importing runs no
host checks. The loop and daemon share these readers (KO-397).
"""
import collections
import math
import re

AGENT_FALLBACK_KEYS = ("implementer_fallback", "reviewer_fallback",
                       "adjudicator_fallback")

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
# A run's hard ceiling, in multiples of its box: whatever the per-turn
# allowance grows to, a run stops here. The loop refuses to arm a turn
# whose budget would carry the run past it -- refused rather than killed
# mid-edit -- and the sweep trips a run that slips past anyway. Bounded
# on both ends: under 1.5 the ceiling sits inside a single turn's grace,
# and past 5 it stops bounding anything.
RUN_CAP = 3.0
RUN_CAP_RANGE = (1.5, 5.0)
# How much of their findings two consecutive review rounds may share before
# the review is read as circling rather than converging: the Jaccard
# overlap `store.findings_overlap()` measures, over the `(path, line,
# severity)` keys the fingerprint hashes. Half, because a fix round that
# leaves half of the reviewer's complaints standing has not moved the
# review, and the round after it is the terminal adjudication -- a doomed
# one is cheaper failed now than paid for. Two rounds are compared and
# never one: a healthy run sits in `reviewing` with a single round on
# file, and there is nothing to compare it against.
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
# is a loop that died in the exec -- the gap every earlier gate passed
# through when the first live re-exec after KO-191 died before printing
# anything. Two minutes: an exec is instant and a startup probe is seconds,
# so a loop silent that long is not merely slow.
RESTART_GRACE_SEC = 120
# How long the supervisor's board fallback waits between two asks for the
# ready listing (KO-434). A Linear ready listing costs thousands of the
# key's hourly complexity points, and a mirror that stays empty does not
# need a fresh answer a minute: ten minutes is the default, and under one
# minute the fallback is the polling that emptied the key.
BOARD_ASK_SEC = 600

# The eight knobs above have an address: the optional `[supervisor]` table of
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
    "run_cap": RUN_CAP,
    "review_overlap_threshold": REVIEW_OVERLAP_THRESHOLD,
    "sweep_interval_sec": SUPERVISE_INTERVAL_SEC,
    "restart_grace_sec": RESTART_GRACE_SEC,
    "board_ask_sec": BOARD_ASK_SEC,
}
# The knobs as the sweep reads them: the same eight, with the heartbeat
# threshold, the restart grace and the board-ask interval already in
# milliseconds, so the arithmetic in `sweep()` is the arithmetic it always
# was.
SweepConfig = collections.namedtuple(
    "SweepConfig",
    ("heartbeat_stale_ms", "stale_strikes", "budget_grace", "run_cap",
     "review_overlap_threshold", "sweep_interval_sec", "restart_grace_ms",
     "board_ask_ms"))


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
        elif key == "run_cap":
            low, high = RUN_CAP_RANGE
            constraint, ok = (f"a number from {low} to {high}",
                              number and low <= value <= high)
        elif key == "board_ask_sec":
            constraint, ok = "an integer of at least 60", number and (
                isinstance(value, int) and value >= 60)
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
        run_cap=values["run_cap"],
        review_overlap_threshold=values["review_overlap_threshold"],
        sweep_interval_sec=values["sweep_interval_sec"],
        restart_grace_ms=values["restart_grace_sec"] * 1000,
        board_ask_ms=values["board_ask_sec"] * 1000)

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
# `review_rounds`, `review_rounds_per_lines`, `review_rounds_max`: the
# review-round cap, computed per run from the candidate's diff as
# `min(review_rounds_max, review_rounds + lines // review_rounds_per_lines)`
# (`holophyte.runs.review_round_cap()`); `review_rounds_per_lines = 0`
# turns the scaling off. The base defaults to `MAX_ROUNDS`, so a target
# with no table pays the two rounds it always has.
# `workers`: the ceiling on the pool of worker processes the loop keeps
# running, one per claimable ticket up to this many (KO-343). `1`, the
# default, is the loop as it has always been: one process, one ticket at
# a time. Above `1` the main process is a scheduler spawning
# `factory.py TARGET --worker` children and works nothing itself.
# `tick_sec`: how often, in seconds, the scheduler recounts the queue while
# the pool is below `workers` (KO-353). A scheduler waiting on child exits
# alone let a ticket filed while the pool was busy wait for the next exit
# with slots idle; with a slot free the wait times out after this long and
# the listing is run again. A full pool waits on exits alone.
LOOP_KEYS = {
    "stop_on_failure": True,
    "order": "identifier",
    "spawn_supervisor": True,
    "review_rounds": 2,
    "review_rounds_per_lines": 800,
    "review_rounds_max": 4,
    "workers": 1, "tick_sec": 120,
    "fix_session": "fresh",
    "review_session": "fresh",
}
LOOP_ORDERS = ("identifier", "priority")
# The keys that must be integers, and the least each may be: a run with no
# review round is not a run, and a ceiling under the base is a cap the
# formula could never reach. `review_rounds_per_lines` may be `0`, the
# documented switch for "never scale". `tick_sec` under 10 is a poll of the
# board, not a tick.
LOOP_INTEGER_FLOORS = {
    "review_rounds": 1,
    "review_rounds_per_lines": 0,
    "review_rounds_max": 1,
    "workers": 1,
    "tick_sec": 10,
}
LoopConfig = collections.namedtuple("LoopConfig", LOOP_KEYS)


def loop_config(target):
    """The target's `[loop]` knobs over the defaults.

    Checked at startup beside `sweep_config()`, the same way: an absent
    table is the defaults exactly, and a present value has to be the type
    the key means. `stop_on_failure` and `spawn_supervisor` are booleans,
    and only booleans -- `"yes"`, `1` and `"false"` are all truthy strings
    or numbers TOML never meant as the answer, and a value the factory
    quietly read as one would run a night nobody chose. `order` is one of
    `LOOP_ORDERS`, and only one of those -- `"urgent"` or `1` names no sort
    the loop has. The three `review_rounds*` keys are integers at or above
    `LOOP_INTEGER_FLOORS` (a boolean is refused too: TOML's `true` is not
    a count), and `review_rounds_max` is at least `review_rounds`, or the
    cap is one the formula could never reach. `workers` is an integer of
    at least 1, the same way: `"3"` is a string and `0` a pool that could
    work nothing. `tick_sec` is an integer of at least 10: `"120"` is a
    string and `5` a poll the board was never meant to answer. The refusal
    names the table, the key and the constraint, like a bad `[supervisor]`
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
        choices = {"order": LOOP_ORDERS, **dict.fromkeys(
            ("fix_session", "review_session"), ("fresh", "resume", "alternate"))}
        if key in choices and value not in choices[key]:
            allowed = " or ".join(f'"{o}"' for o in choices[key])
            raise SystemExit(
                f"[holo2] {target.config_path}: [loop] {key} must be one of "
                f"{allowed}, got {value!r}")
        floor = LOOP_INTEGER_FLOORS.get(key)
        if floor is not None and (isinstance(value, bool)
                                  or not isinstance(value, int)
                                  or value < floor):
            raise SystemExit(
                f"[holo2] {target.config_path}: [loop] {key} must be an "
                f"integer of at least {floor}, got {value!r}")
        values[key] = value
    if values["review_rounds_max"] < values["review_rounds"]:
        raise SystemExit(
            f"[holo2] {target.config_path}: [loop] review_rounds_max must be "
            f"at least review_rounds ({values['review_rounds']}), got "
            f"{values['review_rounds_max']!r}")
    return LoopConfig(**values)


# The board the loop claims from: the Linear project's UUID and the name of
# the team it belongs to (the workflow states are looked up per team). Both
# live in the target's config because a target is a repository plus its
# store plus its config, and the board it is driven from belongs with them:
# read from one process-wide variable, two loops on one host for two targets
# claim from the same project, and the second silently works the first's
# queue. Neither has a default -- a board is one operator's, never this
# file's -- so a loop with no table exits at startup naming the key.
# `label` (KO-432) is the one optional key: set to a non-empty string, it
# names the label a ready issue must carry for the loop to see it at all;
# absent, the board is every ready issue, as it has always been.
BOARD_KEYS = {
    "project_id": None,
    "team": None,
    "label": None,
}
BoardConfig = collections.namedtuple("BoardConfig", BOARD_KEYS)


def board_config(target):
    """The target's `[board]`, or `None` when the table is absent.

    A present table has to carry `project_id` and `team` as non-empty
    strings -- half a board names no project to claim from or no team to
    resolve states in -- and may carry `label` the same way, the one
    optional key (KO-432): `None` when absent, `3` or `""` refused. The
    refusal names the table, the key and the constraint, like a bad
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
    values = {"label": None}  # the one optional key; absent is no filter
    for key in BOARD_KEYS:
        value = table.get(key)
        if value is None and key == "label":
            continue
        if not isinstance(value, str) or not value:
            raise SystemExit(
                f"[holo2] {target.config_path}: [board] {key} must be a "
                f"non-empty string, got {value!r}")
        values[key] = value
    return BoardConfig(**values)


# `approve = "auto"` merges a green, approved candidate; `"human"` parks it
# awaiting operator approval. `review_fixes = true` (KO-663, default false)
# puts fix commits pushed after the release under `"human"` to the covering
# review the auto path runs before the run parks. `mode = "local"` merges
# into local main; `"pr"` pushes the task branch to origin and opens a PR for
# bots and CI. The factory never pushes main. `pr_rounds` (at least 1) caps
# babysit passes before parking the PR on its unresolved threads for the
# operator.
#
# `pr_merge_method` is the `merge_method` the babysitter sends GitHub's merge
# API when it lands a green, quiet pull request under `mode = "pr"`:
# `"merge"` (a merge commit, like the local `--no-ff` merge), `"squash"` or
# `"rebase"`. A repository whose ruleset allows squash only, or requires
# linear history, refuses a merge commit. Validated whatever the mode.
#
# `pr_poll_sec` is the least time, in seconds, between two babysit rounds
# the loop itself starts on one parked pull request (KO-362). Every tick
# reads each parked pull request once anyway, to notice a merge; the same
# read now carries GitHub's `updatedAt` and the thread count, and a pull
# request that moved past what the last babysit pass recorded is sent
# back to the babysitter as `--babysit KO-n` would send it -- but no more
# often than this, per pull request, so a reviewer typing three comments
# in a minute gets one round. An integer of at least 10; default 180.
#
# `pr_quiet_sec`: quiet since GitHub updatedAt before merging (default 300;
# 0 merges as soon as green). `check_wait_sec`: positive pending/quiet wait
# cap, default pr.CHECK_WAIT_S (1800), independently set per target (KO-477).
# `missing_check_sec`: how long a check main requires may report nothing on
# the head before the wait stops for it (default 600); with
# `retrigger_missing_checks = true` one empty commit per candidate wakes it
# first, otherwise the run parks naming the checks (KO-652).
#
# Pull request titles and bodies are always written by one implementer turn
# from the diff, ticket and repository conventions. `pr_style` supplies
# optional instructions. An unusable reply falls back to a Summary stub.
#
# `human_threads` is what the babysitter does with a review thread a person
# opened: `"park"` (the default, KO-327) is HUMAN before the adjudicator is
# asked -- no reply, the run parks; `"act"` (KO-337) judges it beside the
# bots' threads, fixes and answers an ADDRESS with the sha, and hands
# anything else to the operator unanswered. The factory never declines a
# person and never resolves their thread, whichever the setting.
#
# `after` is a list of shell strings, default empty, run in order in the main
# checkout once a local merge has landed and before the run is marked merged
# (KO-347): the build nobody remembers to run, `bun --cwd=console run build`,
# so a merge that changes the console's source reaches the bundle the daemon
# serves. The first nonzero exit stops the list and parks the run for the
# operator with the command's output; it does not undo the merge. Not run
# under `mode = "pr"`, where nothing lands in the checkout.
DEFAULT_STRIP_ATTRIBUTION = (
    r"(?i)^Co-Authored-By:\s*(?:(?:Claude|Devin|Codex|Copilot|Cursor)"
    r"(?:\s+(?:Code|AI|Bot))?\s*(?:<|$)|.*(?:noreply@anthropic\.com|devin-ai-integration))",
    r"(?i)^(?:🤖\s*)?Generated with\b.*https?://(?:[a-z0-9-]+\.)*"
    r"(?:anthropic\.com|claude\.(?:ai|com)|devin\.ai|openai\.com|"
    r"copilot\.github\.com|github\.com/(?:features/)?copilot|cursor\.(?:com|sh))\b",
    r"(?i)^🤖\s*Generated with\b",
)
MERGE_KEYS = {
    "strip_attribution": DEFAULT_STRIP_ATTRIBUTION,
    "approve": "auto", "mode": "local",
    "pr_rounds": 5,
    "pr_merge_method": "merge",
    "pr_poll_sec": 180,
    "pr_quiet_sec": 300,
    "check_wait_sec": None,  # Resolved from pr.CHECK_WAIT_S by merge_config.
    "missing_check_sec": 600, "retrigger_missing_checks": False,
    "pr_style": "", "pr_changes_log": False, "review_fixes": False,
    "ui_paths": (), "ui_capture": "", "ui_capture_dir": "e2e/capture",
    "media_repo": "",
    "media_bucket": None, "media_max_file_mb": 10, "media_max_total_mb": 20,
    "human_threads": "park", "bot_threads": "act", "bot_logins": (),
    "mention_handle": "holophyte", "mention_accounts": (),
    "after": (), "bot_authors": ("devin-ai-integration", "coderabbitai",
                               "greptile-apps", "github-actions"),
}
MERGE_APPROVALS = ("auto", "human")
MERGE_MODES = ("local", "pr")
MERGE_METHODS = ("merge", "squash", "rebase")
MERGE_HUMAN_THREADS = ("park", "act")
MERGE_VALUES = {"approve": MERGE_APPROVALS, "mode": MERGE_MODES,
               "pr_merge_method": MERGE_METHODS,
               "human_threads": MERGE_HUMAN_THREADS, "bot_threads": ("act", "advisory")}
MergeConfig = collections.namedtuple("MergeConfig", tuple(MERGE_KEYS))
PR_POLL_FLOOR = 10
MERGE_INT_FLOORS = {"pr_rounds": 1, "pr_poll_sec": PR_POLL_FLOOR,
                    "pr_quiet_sec": 0, "check_wait_sec": 1,
                    "missing_check_sec": 1}


def merge_config(target):
    """Validate merge settings at startup; refusals name the config and key."""
    from holophyte.pr import CHECK_WAIT_S  # Deferred: pr also reads config.
    table = target.config().get("merge", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {target.config_path}: [merge] must be a table, got "
            f"{type(table).__name__}")
    if "pr_text" in table:
        raise SystemExit(
            f"[holo2] {target.config_path}: [merge] pr_text was retired:"
            " pull request bodies are always written")
    values = {}
    defaults = dict(MERGE_KEYS, check_wait_sec=CHECK_WAIT_S)
    values["strip_attribution"] = _attribution_patterns(
        target, table.get("strip_attribution", defaults.pop("strip_attribution")))
    values["pr_changes_log"] = _merge_boolean(
        target, "pr_changes_log",
        table.get("pr_changes_log", defaults.pop("pr_changes_log")))
    values["review_fixes"] = _merge_boolean(
        target, "review_fixes",
        table.get("review_fixes", defaults.pop("review_fixes")))
    values["retrigger_missing_checks"] = _merge_boolean(
        target, "retrigger_missing_checks", table.get(
            "retrigger_missing_checks", defaults.pop("retrigger_missing_checks")))
    for key, default in defaults.items():
        value = table.get(key, default)
        if key in ("media_bucket", "media_max_file_mb", "media_max_total_mb"):
            values[key] = _media_setting(target, key, value)
            continue
        if key in MERGE_INT_FLOORS:
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < MERGE_INT_FLOORS[key]:
                raise SystemExit(
                    f"[holo2] {target.config_path}: [merge] {key} must be an"
                    f" integer of at least {MERGE_INT_FLOORS[key]},"
                    f" got {value!r}")
            values[key] = value
            continue
        if key in ("pr_style", "mention_handle", "ui_capture",
                   "ui_capture_dir", "media_repo"):
            if not isinstance(value, str):
                raise SystemExit(
                    f"[holo2] {target.config_path}: [merge] {key} must be a"
                    " string" + (" in owner/name form" if key == "media_repo" else "")
                    + f", got {value!r}")
            values[key] = value
            continue
        if key in ("after", "bot_authors", "bot_logins", "ui_paths",
                   "mention_accounts"):
            if not isinstance(value, (list, tuple)) \
                    or not all(isinstance(cmd, str) for cmd in value):
                raise SystemExit(
                    f"[holo2] {target.config_path}: [merge] {key} must be a"
                    f" list of strings, got {value!r}")
            values[key] = tuple(value)
            continue
        if value not in MERGE_VALUES[key]:
            allowed = " or ".join(f'"{o}"' for o in MERGE_VALUES[key])
            raise SystemExit(
                f"[holo2] {target.config_path}: [merge] {key} must be one of "
                f"{allowed}, got {value!r}")
        values[key] = value
    _validate_ui(target, values)
    return MergeConfig(**values)


def _merge_boolean(target, key, value):
    if not isinstance(value, bool):
        raise SystemExit(
            f"[holo2] {target.config_path}: [merge] {key} must be a"
            f" boolean, got {value!r}")
    return value


def _attribution_patterns(target, value):
    error = None
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(p, str) for p in value):
        error = "must be a list of regular expressions"
    else:
        try:
            for pattern in value:
                re.compile(pattern)
        except re.error as exc:
            error = f"invalid regular expression: {exc}"
    if error:
        raise SystemExit(f"[holo2] {target.config_path}: "
                         f"[merge] strip_attribution {error}")
    return tuple(value)


def _media_setting(target, key, value):
    from holophyte.media_store import validate_bucket
    try:
        if key == "media_bucket":
            return None if value is None else validate_bucket(value)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{key} must be a positive finite number of MB")
        return value
    except ValueError as error:
        raise SystemExit(f"{target.config_path}: [merge] {error}") from None


def _validate_ui(target, values):
    import shlex
    from pathlib import PurePosixPath
    if values["media_repo"] and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.\.?$)[A-Za-z0-9_.-]+", values["media_repo"]):
        raise SystemExit(f"{target.config_path}: [merge] media_repo"
                         " must be in owner/name form")
    paths, command = values["ui_paths"], values["ui_capture"]
    if bool(paths) != bool(command.strip()):
        raise SystemExit("[merge] ui_paths and ui_capture must be configured together")
    if any(not p.strip() or PurePosixPath(p).is_absolute()
           or ".." in PurePosixPath(p).parts for p in paths):
        raise SystemExit("[merge] ui_paths must be non-empty repository-relative globs")
    try:
        args = shlex.split(command)
    except ValueError as error:
        raise SystemExit(f"[merge] ui_capture: {error}") from None
    if command and (not args or not args[0]):
        raise SystemExit("[merge] ui_capture must name a command")


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
#
# `findings` is whether the loop renders FINDINGS.md at all: `none`, the
# default, renders and commits nothing -- the store is the record, read
# through the console or `--report`; `repo` renders and commits the bounded
# window at every close-out, for a target that wants the evidence beside
# its code. The ledger lives in the store either way (design note 9) and
# the daemon serves it from `/runs/N/ledger`; the file is a projection a
# target opts into, and a copy that can drift from the store is worse than
# none.
FINDINGS_MODES = ("none", "repo")
REPORT_KEYS = {
    "host_label": None,
    "findings": "none",
}
ReportConfig = collections.namedtuple("ReportConfig",
                                      ("host_label", "findings"))


def report_config(target):
    """The target's `[report]` knobs over the defaults.

    Checked at startup beside `loop_config()`, the same way: an absent table
    (or key) is the defaults exactly -- no label, the hostname rendered as
    it always was -- and a present `host_label` has to be a string, and a
    non-empty one: `3` names no writer, and `""` would render every host as
    nothing, which is the invisible blank `host_name()`'s `?` exists to
    avoid. `findings` is one of `FINDINGS_MODES`, `none` when absent. The
    refusal names the table, the key and the constraint, like a bad
    `[loop]` value. Keys this version does not know are refused by
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
    if values["findings"] not in FINDINGS_MODES:
        allowed = ", ".join(FINDINGS_MODES)
        raise SystemExit(
            f"[holo2] {target.config_path}: [report] findings must be one of "
            f"{allowed}, got {values['findings']!r}")
    return ReportConfig(**values)


# Where the other daemons are. One daemon serves one project; the console
# shows every project on every host, so the page needs to be told where to
# fan out, and the daemon it was loaded from tells it (`GET /peers`). The
# entries are `HOST:PORT` strings the page fetches, never addresses this
# daemon connects to. Config on the writer host is where the operator
# already writes such things (design note 13).
CONSOLE_KEYS = {
    "daemons": (),
}


def split_address(text):
    """`HOST:PORT` as a `(host, port)` pair; ValueError otherwise.

    The host is whatever precedes the last colon, non-empty; the port a
    decimal integer. Nothing here decides what a valid hostname is: the
    bind (or the browser's fetch) does. `--serve` and `[console] daemons`
    hold their addresses to this one rule, so an entry the console cannot
    reach for want of a port is refused where `--serve` would refuse it.
    """
    host, sep, port = str(text).rpartition(":")
    if not sep or not host or not port.isdecimal():
        raise ValueError(f"expected HOST:PORT, got {text!r}")
    return host, int(port)


VerifyConfig = collections.namedtuple("VerifyConfig", "always before_merge timeout_sec")


def verify_config(target):
    """Validate baseline command shapes at startup; execute them only at gates."""
    from holophyte.config import VERIFY_TIMEOUT, config_table

    table = config_table(target, "verify")
    commands = {}
    for tier in ("always", "before_merge"):
        value = table.get(tier, [])
        if not isinstance(value, list) or any(
                not isinstance(cmd, str) or not cmd.strip() for cmd in value):
            raise SystemExit(
                f"[holo2] {target.config_path}: [verify] {tier} must be a "
                "list of non-empty command strings")
        commands[tier] = value
    timeout = table.get("timeout_sec", VERIFY_TIMEOUT)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise SystemExit(
            f"[holo2] {target.config_path}: [verify] timeout_sec must be a "
            "finite positive number of seconds")
    return VerifyConfig(**commands, timeout_sec=timeout)
