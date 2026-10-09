import collections
import math
import re

AGENT_FALLBACK_KEYS = ("implementer_fallback", "reviewer_fallback",
                       "adjudicator_fallback", "trimmer_fallback",
                       "adversary_fallback")

HEARTBEAT_STALE_MS = 5 * 60 * 1000
STALE_STRIKES = 2
BUDGET_GRACE = 1.5
RUN_CAP = 3.0
RUN_CAP_RANGE = (1.5, 5.0)
REVIEW_OVERLAP_THRESHOLD = 0.5
SUPERVISE_INTERVAL_SEC = 60
RESTART_GRACE_SEC = 120
BOARD_ASK_SEC = 600

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
SweepConfig = collections.namedtuple(
    "SweepConfig",
    ("heartbeat_stale_ms", "stale_strikes", "budget_grace", "run_cap",
     "review_overlap_threshold", "sweep_interval_sec", "restart_grace_ms",
     "board_ask_ms"))


def sweep_config(project):
    table = project.config().get("supervisor", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [supervisor] must be a table, got "
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
                f"[holo2] {project.config_path}: [supervisor] {key} must be "
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
    "critic_after_hours": 12,
}
LOOP_ORDERS = ("identifier", "priority")
LOOP_INTEGER_FLOORS = {
    "review_rounds": 1,
    "review_rounds_per_lines": 0,
    "review_rounds_max": 1,
    "workers": 1,
    "tick_sec": 10,
    "critic_after_hours": 0,
}
LoopConfig = collections.namedtuple("LoopConfig", LOOP_KEYS)


def loop_config(project):
    table = project.config().get("loop", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [loop] must be a table, got "
            f"{type(table).__name__}")
    values = {}
    for key, default in LOOP_KEYS.items():
        value = table.get(key, default)
        if isinstance(default, bool) and not isinstance(value, bool):
            raise SystemExit(
                f"[holo2] {project.config_path}: [loop] {key} must be a boolean "
                f"(true or false), got {value!r}")
        choices = {"order": LOOP_ORDERS, **dict.fromkeys(
            ("fix_session", "review_session"), ("fresh", "resume", "alternate"))}
        if key in choices and value not in choices[key]:
            allowed = " or ".join(f'"{o}"' for o in choices[key])
            raise SystemExit(
                f"[holo2] {project.config_path}: [loop] {key} must be one of "
                f"{allowed}, got {value!r}")
        floor = LOOP_INTEGER_FLOORS.get(key)
        if floor is not None and (isinstance(value, bool)
                                  or not isinstance(value, int)
                                  or value < floor):
            raise SystemExit(
                f"[holo2] {project.config_path}: [loop] {key} must be an "
                f"integer of at least {floor}, got {value!r}")
        values[key] = value
    if values["review_rounds_max"] < values["review_rounds"]:
        raise SystemExit(
            f"[holo2] {project.config_path}: [loop] review_rounds_max must be "
            f"at least review_rounds ({values['review_rounds']}), got "
            f"{values['review_rounds_max']!r}")
    return LoopConfig(**values)


STORY_KEYS = {"witness_sec": 600, "max_parallel": 2,
              "dependency_ready": "merged"}
STORY_CHOICES = {"dependency_ready": ("merged",)}
StoryConfig = collections.namedtuple("StoryConfig", STORY_KEYS)


def story_config(project):
    table = project.config().get("story", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [story] must be a table, got "
            f"{type(table).__name__}")
    values = {}
    for key, default in STORY_KEYS.items():
        value = table.get(key, default)
        if key in STORY_CHOICES:
            if value not in STORY_CHOICES[key]:
                raise SystemExit(
                    f"[holo2] {project.config_path}: [story] {key} must be "
                    f"one of {', '.join(STORY_CHOICES[key])}, got {value!r}")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SystemExit(
                f"[holo2] {project.config_path}: [story] {key} must be a "
                f"positive integer, got {value!r}")
        values[key] = value
    return StoryConfig(**values)


TRIM_KEYS = {"enabled": True, "budget_min": 15}
TrimConfig = collections.namedtuple("TrimConfig", TRIM_KEYS)


def trim_config(project):
    table = project.config().get("trim", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [trim] must be a table, got "
            f"{type(table).__name__}")
    enabled = table.get("enabled", TRIM_KEYS["enabled"])
    if not isinstance(enabled, bool):
        raise SystemExit(
            f"[holo2] {project.config_path}: [trim] enabled must be a boolean "
            f"(true or false), got {enabled!r}")
    budget = table.get("budget_min", TRIM_KEYS["budget_min"])
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise SystemExit(
            f"[holo2] {project.config_path}: [trim] budget_min must be an "
            f"integer of at least 1, got {budget!r}")
    return TrimConfig(enabled=enabled, budget_min=budget)


BOARD_KEYS = {
    "project_id": None,
    "team": None,
    "label": None,
    "prefix": None,
}
BOARD_PREFIX_ALIAS = "key"
BoardConfig = collections.namedtuple("BoardConfig", BOARD_KEYS)
# The shape every parser of a `KEY-n` identifier accepts.
NATIVE_KEY_SHAPE = re.compile(r"[A-Z][A-Z0-9]{0,9}")


def _board_string(project, table, key):
    value = table.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise SystemExit(
            f"[holo2] {project.config_path}: [board] {key} must be a "
            f"non-empty string, got {value!r}")
    return value


def board_config(project):
    table = project.config().get("board")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [board] must be a table, got "
            f"{type(table).__name__}")
    kind = board_mode(project).kind
    values = {key: _board_string(project, table, key) for key in BOARD_KEYS}
    alias = _board_string(project, table, BOARD_PREFIX_ALIAS)
    required = ("prefix",) if kind == "native" else ("project_id", "team")
    refused = (("project_id", "label") if kind == "native"
               else ("prefix", BOARD_PREFIX_ALIAS))
    for key in refused:
        if table.get(key) is not None:
            raise SystemExit(
                f"[holo2] {project.config_path}: [board] {key} is not read "
                f"by a {kind} board; remove it")
    if alias is not None and values["prefix"] not in (None, alias):
        raise SystemExit(
            f"[holo2] {project.config_path}: [board] prefix "
            f"{values['prefix']!r} and its deprecated alias [board] key "
            f"{alias!r} differ; keep prefix and remove key")
    named = "prefix" if values["prefix"] is not None or alias is None else "key"
    values["prefix"] = values["prefix"] or alias
    for key in required:
        if values[key] is None:
            raise SystemExit(
                f"[holo2] {project.config_path}: [board] {key} must be a "
                f"non-empty string, got None")
    if kind == "native":
        if not NATIVE_KEY_SHAPE.fullmatch(values["prefix"]):
            raise SystemExit(
                f"[holo2] {project.config_path}: [board] {named} must be an "
                "uppercase letter then up to nine uppercase letters or "
                f"digits, got {values['prefix']!r}")
        values["team"] = values["team"] or f"native:{values['prefix']}"
    return BoardConfig(**values)


BOARD_MODE_KEYS = {
    "mode": "mirror",
    "kind": "linear",
}
BOARD_MODE_VALUES = {
    "mode": ("mirror", "store"),
    "kind": ("linear", "native"),
}
BoardMode = collections.namedtuple("BoardMode", BOARD_MODE_KEYS)


def board_mode(project):
    table = project.config().get("board", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [board] must be a table, got "
            f"{type(table).__name__}")
    defaults = dict(BOARD_MODE_KEYS)
    values = {}
    for key in ("kind", "mode"):
        value = table.get(key, defaults[key])
        if value not in BOARD_MODE_VALUES[key]:
            allowed = " or ".join(f'"{o}"' for o in BOARD_MODE_VALUES[key])
            raise SystemExit(
                f"[holo2] {project.config_path}: [board] {key} must be one of "
                f"{allowed}, got {value!r}")
        values[key] = value
        if value == "native":
            defaults["mode"] = "store"
    if values["kind"] == "native" and values["mode"] == "mirror":
        raise SystemExit(
            f"[holo2] {project.config_path}: [board] mode must be \"store\" "
            "for a native board, got 'mirror'")
    return BoardMode(**values)


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
    "private_patterns": (),
    "approve": "auto", "mode": "local",
    "pr_rounds": 5, "pr_main_refreshes": 10,
    "pr_merge_method": "merge",
    "pr_poll_sec": 180,
    "pr_quiet_sec": 300,
    "check_wait_sec": None,  # Resolved from github.CHECK_WAIT_S by merge_config.
    "missing_check_sec": 600, "retrigger_missing_checks": False,
    "require_up_to_date": True,
    "pr_style": "", "pr_changes_log": False, "review_fixes": False,
    "ui_paths": (), "ui_capture": "", "ui_capture_dir": "e2e/capture",
    "ui_capture_local": False,
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
MERGE_INT_FLOORS = {"pr_rounds": 1, "pr_main_refreshes": 1,
                    "pr_poll_sec": PR_POLL_FLOOR,
                    "pr_quiet_sec": 0, "check_wait_sec": 1,
                    "missing_check_sec": 1}


def merge_config(project):
    from holophyte.pr.github import CHECK_WAIT_S  # Deferred: github also reads config.
    table = project.config().get("merge", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [merge] must be a table, got "
            f"{type(table).__name__}")
    if "pr_text" in table:
        raise SystemExit(
            f"[holo2] {project.config_path}: [merge] pr_text was retired:"
            " pull request bodies are always written")
    values = {}
    defaults = dict(MERGE_KEYS, check_wait_sec=CHECK_WAIT_S)
    values["strip_attribution"] = _attribution_patterns(
        project, table.get("strip_attribution", defaults.pop("strip_attribution")))
    values["private_patterns"] = _private_patterns(
        project, table.get("private_patterns", defaults.pop("private_patterns")))
    values["pr_changes_log"] = _merge_boolean(
        project, "pr_changes_log",
        table.get("pr_changes_log", defaults.pop("pr_changes_log")))
    values["review_fixes"] = _merge_boolean(
        project, "review_fixes",
        table.get("review_fixes", defaults.pop("review_fixes")))
    values["retrigger_missing_checks"] = _merge_boolean(
        project, "retrigger_missing_checks", table.get(
            "retrigger_missing_checks", defaults.pop("retrigger_missing_checks")))
    values["require_up_to_date"] = _merge_boolean(
        project, "require_up_to_date", table.get(
            "require_up_to_date", defaults.pop("require_up_to_date")))
    values["ui_capture_local"] = _merge_boolean(
        project, "ui_capture_local",
        table.get("ui_capture_local", defaults.pop("ui_capture_local")))
    for key, default in defaults.items():
        value = table.get(key, default)
        if key in ("media_bucket", "media_max_file_mb", "media_max_total_mb"):
            values[key] = _media_setting(project, key, value)
            continue
        if key in MERGE_INT_FLOORS:
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < MERGE_INT_FLOORS[key]:
                raise SystemExit(
                    f"[holo2] {project.config_path}: [merge] {key} must be an"
                    f" integer of at least {MERGE_INT_FLOORS[key]},"
                    f" got {value!r}")
            values[key] = value
            continue
        if key in ("pr_style", "mention_handle", "ui_capture",
                   "ui_capture_dir", "media_repo"):
            if not isinstance(value, str):
                raise SystemExit(
                    f"[holo2] {project.config_path}: [merge] {key} must be a"
                    " string" + (" in owner/name form" if key == "media_repo" else "")
                    + f", got {value!r}")
            values[key] = value
            continue
        if key in ("after", "bot_authors", "bot_logins", "ui_paths",
                   "mention_accounts"):
            if not isinstance(value, (list, tuple)) \
                    or not all(isinstance(cmd, str) for cmd in value):
                raise SystemExit(
                    f"[holo2] {project.config_path}: [merge] {key} must be a"
                    f" list of strings, got {value!r}")
            values[key] = tuple(value)
            continue
        if value not in MERGE_VALUES[key]:
            allowed = " or ".join(f'"{o}"' for o in MERGE_VALUES[key])
            raise SystemExit(
                f"[holo2] {project.config_path}: [merge] {key} must be one of "
                f"{allowed}, got {value!r}")
        values[key] = value
    _validate_ui(project, values)
    return MergeConfig(**values)


def _merge_boolean(project, key, value):
    if not isinstance(value, bool):
        raise SystemExit(
            f"[holo2] {project.config_path}: [merge] {key} must be a"
            f" boolean, got {value!r}")
    return value


def _attribution_patterns(project, value):
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
        raise SystemExit(f"[holo2] {project.config_path}: "
                         f"[merge] strip_attribution {error}")
    return tuple(value)


def _private_patterns(project, value):
    from holophyte.redact import register_values
    where = f"[holo2] {project.config_path}: [merge] private_patterns"
    if not isinstance(value, (list, tuple)):
        raise SystemExit(f"{where} must be a list of regular expressions")
    register_values([p for p in value if isinstance(p, str)])
    for index, pattern in enumerate(value):
        if not isinstance(pattern, str):
            raise SystemExit(f"{where} #{index} is not a string")
        try:
            re.compile(pattern)
        except re.error:
            raise SystemExit(f"{where} #{index} is not a valid regular"
                             " expression") from None
    return tuple(value)


def _media_setting(project, key, value):
    from holophyte.media_store import validate_bucket
    try:
        if key == "media_bucket":
            return None if value is None else validate_bucket(value)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{key} must be a positive finite number of MB")
        return value
    except ValueError as error:
        raise SystemExit(f"{project.config_path}: [merge] {error}") from None


def _validate_ui(project, values):
    import shlex
    from pathlib import PurePosixPath
    if values["media_repo"] and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.\.?$)[A-Za-z0-9_.-]+", values["media_repo"]):
        raise SystemExit(f"{project.config_path}: [merge] media_repo"
                         " must be in owner/name form")
    paths, command = values["ui_paths"], values["ui_capture"]
    if bool(paths) != bool(command.strip()):
        raise SystemExit("[merge] ui_paths and ui_capture must be configured together")
    if any(not p.strip() or PurePosixPath(p).is_absolute()
           or ".." in PurePosixPath(p).parts for p in paths):
        raise SystemExit("[merge] ui_paths must be non-empty repository-relative globs")
    directory = PurePosixPath(values["ui_capture_dir"])
    if values["ui_capture_local"] and (
            directory.is_absolute() or not directory.parts
            or ".." in directory.parts):
        raise SystemExit(
            f"{project.config_path}: [merge] ui_capture_local needs ui_capture_dir"
            " to be a repository-relative directory without `..`, got"
            f" {values['ui_capture_dir']!r}")
    try:
        args = shlex.split(command)
    except ValueError as error:
        raise SystemExit(f"[merge] ui_capture: {error}") from None
    if command and (not args or not args[0]):
        raise SystemExit("[merge] ui_capture must name a command")


FINDINGS_MODES = ("none", "repo")
REPORT_KEYS = {
    "host_label": None,
    "findings": "none",
}
ReportConfig = collections.namedtuple("ReportConfig",
                                      ("host_label", "findings"))


def report_config(project):
    table = project.config().get("report", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [report] must be a table, got "
            f"{type(table).__name__}")
    values = {}
    for key, default in REPORT_KEYS.items():
        value = table.get(key, default)
        if value is not None and not (isinstance(value, str) and value.strip()):
            raise SystemExit(
                f"[holo2] {project.config_path}: [report] {key} must be a "
                f"non-empty string, got {value!r}")
        values[key] = value
    if values["findings"] not in FINDINGS_MODES:
        allowed = ", ".join(FINDINGS_MODES)
        raise SystemExit(
            f"[holo2] {project.config_path}: [report] findings must be one of "
            f"{allowed}, got {values['findings']!r}")
    return ReportConfig(**values)


CONSOLE_KEYS = {
    "daemons": (),
}


def split_address(text):
    host, sep, port = str(text).rpartition(":")
    if not sep or not host or not port.isdecimal():
        raise ValueError(f"expected HOST:PORT, got {text!r}")
    return host, int(port)


VerifyConfig = collections.namedtuple("VerifyConfig", "always before_merge timeout_sec")


def verify_config(project):
    # Deferred: the reader imports this module.
    from holophyte.config.reader import VERIFY_TIMEOUT, config_table

    table = config_table(project, "verify")
    commands = {}
    for tier in ("always", "before_merge"):
        value = table.get(tier, [])
        if not isinstance(value, list) or any(
                not isinstance(cmd, str) or not cmd.strip() for cmd in value):
            raise SystemExit(
                f"[holo2] {project.config_path}: [verify] {tier} must be a "
                "list of non-empty command strings")
        commands[tier] = value
    timeout = table.get("timeout_sec", VERIFY_TIMEOUT)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise SystemExit(
            f"[holo2] {project.config_path}: [verify] timeout_sec must be a "
            "finite positive number of seconds")
    return VerifyConfig(**commands, timeout_sec=timeout)
