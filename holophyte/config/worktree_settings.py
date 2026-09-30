import math
import pathlib
import re
from pathlib import Path

from holophyte.config.reader import VERIFY_TIMEOUT, config_table


def setup_commands(project):
    commands = config_table(project, "worktree").get("setup")
    if commands is None:
        return []
    if not isinstance(commands, list):
        raise SystemExit(
            f"[holo2] {project.config_path}: [worktree] setup must be a list of "
            f"command strings, got {type(commands).__name__}")
    for command in commands:
        if not isinstance(command, str):
            raise SystemExit(
                f"[holo2] {project.config_path}: [worktree] setup: every entry must be "
                f"a command string, got {type(command).__name__}")
        if not command.strip():
            raise SystemExit(
                f"[holo2] {project.config_path}: [worktree] setup: entry {command!r} "
                "is empty")
    return commands


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_environment(text, label="env_source"):
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


def process_value(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def worktree_environment(project):
    return allowed_environment(project, "worktree", "env_source", "env_allow")


def capture_environment(project):
    values = allowed_environment(project, "merge", "capture_env_source",
                                 "capture_env_allow")
    return None if values is None else {
        name: process_value(value) for name, value in values.items()}


def allowed_environment(project, name, source_key, allow_key):
    from holophyte.redact import register_values

    table = config_table(project, name)
    if source_key not in table and allow_key not in table:
        return None
    prefix = f"[holo2] {project.config_path}: [{name}] "
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
        path = Path(project.config_path).parent / path
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


def setup_timeout(project):
    value = config_table(project, "worktree").get("setup_timeout_sec")
    if value is None:
        return VERIFY_TIMEOUT
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise SystemExit(
            f"[holo2] {project.config_path}: [worktree] setup_timeout_sec must be a "
            f"finite positive number of seconds, got {value!r}")
    return value


def carry_directories(project):
    entries = config_table(project, "worktree").get("carry")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise SystemExit(
            f"[holo2] {project.config_path}: [worktree] carry must be a list of "
            f"repository-relative directories, got {type(entries).__name__}")
    for entry in entries:
        if not isinstance(entry, str):
            raise SystemExit(
                f"[holo2] {project.config_path}: [worktree] carry: every entry must "
                f"be a repository-relative directory, got {type(entry).__name__}")
        parts = pathlib.PurePosixPath(entry).parts
        if (not entry.strip() or entry.startswith("/") or not parts
                or ".." in parts):
            raise SystemExit(
                f"[holo2] {project.config_path}: [worktree] carry: entry {entry!r} "
                "must be a relative path inside the repository")
    return entries


# Characters git refuses in a ref, plus the slash: one segment before the ident.
BRANCH_PREFIX_REFUSED = set("/~^:?*[\\")
DEFAULT_BRANCH_PREFIX = "task"


def branch_prefix(project):
    value = config_table(project, "worktree").get("branch_prefix")
    if value is None:
        return DEFAULT_BRANCH_PREFIX
    if not isinstance(value, str):
        raise SystemExit(
            f"[holo2] {project.config_path}: [worktree] branch_prefix must be a "
            f"string, got {type(value).__name__}")
    if not value:
        raise SystemExit(
            f"[holo2] {project.config_path}: [worktree] branch_prefix must not be "
            "empty")
    if (any(c.isspace() or c in BRANCH_PREFIX_REFUSED or ord(c) < 0x20 or c == "\x7f"
            for c in value)
            or value.startswith((".", "-")) or value.endswith((".", ".lock"))
            or ".." in value or "@{" in value or value == "@"):
        raise SystemExit(
            f"[holo2] {project.config_path}: [worktree] branch_prefix {value!r} is "
            "not a legal branch segment: no whitespace, no '/', none of "
            "~ ^ : ? * [ \\, no leading '.' or '-', no '..', and not ending in "
            "'.' or '.lock'")
    return value
