from pathlib import PurePosixPath

from holophyte.config.reader import load_config
from holophyte.redact import known_secrets, outbound, redact_document

NOTE_TARGETS = ("implement", "review", "trim", "critic", "pr_writer",
                "babysit_reply")
NOTE_LIMIT = 4000


def current_notes(project):
    config = load_config(project.config_path)
    secrets = known_secrets(config) | known_secrets(project.config())
    return {key: outbound(text, secrets)
            for key, text in pipeline_notes(project, config).items()}


def pipeline_notes(project, config=None):
    config = project.config() if config is None else config
    table = config.get("pipeline", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [pipeline] must be a table, got "
            f"{type(table).__name__}")
    notes = table.get("notes", {})
    accepts = f"[pipeline.notes] accepts: {', '.join(sorted(NOTE_TARGETS))}"
    if not isinstance(notes, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [pipeline] notes must be a table, "
            f"got {type(notes).__name__}; {accepts}")
    found = {}
    for key, value in notes.items():
        if key not in NOTE_TARGETS:
            problem = "unknown key"
        elif not isinstance(value, str):
            problem = f"must be a string, got {type(value).__name__}"
        elif len(value) > NOTE_LIMIT:
            problem = (f"is {len(value)} characters, over the {NOTE_LIMIT} "
                       "allowed")
        else:
            if value.strip():
                found[key] = value
            continue
        raise SystemExit(f"[holo2] {project.config_path}: [pipeline.notes] "
                         f"{key}: {problem}; {accepts}")
    return found


CHECK_KEYS = ("id", "after", "commands", "mode", "paths")
CHECK_AFTER = ("implement", "trim")
CHECK_MODES = ("advisory", "off", "required")
STANDARD_STAGES = ("critic", "implement", "trim", "verify", "review",
                   "merge_gate", "capture", "covering_fix", "check_fix",
                   "capture_fix", "waiting")


def current_checks(project):
    return pipeline_checks(project, load_config(project.config_path))


def check_secrets(project):
    try:
        disk = known_secrets(load_config(project.config_path))
    except (OSError, UnicodeError, SystemExit):
        disk = frozenset()
    return disk | known_secrets(project.config())


def pinned_checks(project):
    config = load_config(project.config_path)
    checks = pipeline_checks(project, config)
    raw = [check for after in CHECK_AFTER for check in checks
           if check["after"] == after]
    secrets = known_secrets(config) | known_secrets(project.config())
    shown = []
    for check in raw:
        commands = [outbound(command, secrets) for command in check["commands"]]
        shown.append({**check, "commands": commands,
                      "redacted": redact_document(commands) != check["commands"]})
    return raw, shown, secrets


def pipeline_checks(project, config=None):
    config = project.config() if config is None else config
    table = config.get("pipeline", {})
    entries = table.get("check", []) if isinstance(table, dict) else []
    if not isinstance(entries, list):
        raise SystemExit(
            f"[holo2] {project.config_path}: [[pipeline.check]] must be an "
            f"array of tables, got {type(entries).__name__}")
    found = []
    for at, entry in enumerate(entries, 1):
        name = (entry.get("id") if isinstance(entry, dict)
                and isinstance(entry.get("id"), str) else f"entry {at}")
        problem = _check_problem(entry, [check["id"] for check in found])
        if problem:
            raise SystemExit(f"[holo2] {project.config_path}: "
                             f"[[pipeline.check]] {name}: {problem}")
        found.append({"id": entry["id"], "after": entry["after"],
                      "mode": entry["mode"],
                      "commands": list(entry["commands"]),
                      "paths": list(entry.get("paths", []))})
    return found


def _check_problem(entry, earlier):
    if not isinstance(entry, dict):
        return f"must be a table, got {type(entry).__name__}"
    unknown = sorted(set(entry) - set(CHECK_KEYS))
    if unknown:
        return (f"unknown key {unknown[0]}; [[pipeline.check]] accepts: "
                f"{', '.join(CHECK_KEYS)}")
    stage_id = entry.get("id")
    if not isinstance(stage_id, str) or not stage_id.strip():
        return "id must be a non-empty string"
    if stage_id in earlier:
        return "id repeats an earlier [[pipeline.check]]"
    if stage_id in STANDARD_STAGES:
        return f"id {stage_id} is a standard stage's id"
    return _settings_problem(entry)


def _settings_problem(entry):
    commands, paths = entry.get("commands"), entry.get("paths", [])
    if entry.get("after") not in CHECK_AFTER:
        return f"after must be one of {', '.join(CHECK_AFTER)}"
    if (not isinstance(commands, list) or not commands or any(
            not isinstance(command, str) or not command.strip()
            for command in commands)):
        return "commands must be a non-empty list of non-empty strings"
    if entry.get("mode") not in CHECK_MODES:
        return f"mode must be one of {', '.join(CHECK_MODES)}"
    if not isinstance(paths, list) or any(
            not isinstance(path, str) or not path.strip()
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts for path in paths):
        return ("paths must be a list of non-empty repository-relative globs "
                "without `..`")
    return None
