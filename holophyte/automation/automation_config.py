"""`[automations]`: which rules a project opts into, and in what mode."""
from typing import NamedTuple

MAIN_WATCH = "main_watch"
SMOKE = "smoke"
OFF, DRY_RUN, ACT = "off", "dry_run", "act"
MAIN_WATCH_MODES = (OFF, DRY_RUN, ACT)
AUTOMATION_KEYS = {MAIN_WATCH: frozenset({"mode", "revision"}),
                   SMOKE: frozenset({"mode", "revision", "command",
                                     "every_hours"})}
BUILT_INS = tuple(AUTOMATION_KEYS)


class WatchConfig(NamedTuple):
    mode: str
    revision: int | None


class SmokeConfig(NamedTuple):
    mode: str
    revision: int | None
    command: str | None
    every_hours: int | None


def rule_table(project, name):
    table = project.config().get("automations", {})
    if not isinstance(table, dict):
        raise ValueError(f"[automations] must be a table, got"
                         f" {type(table).__name__}")
    rule = table.get(name, {})
    if not isinstance(rule, dict):
        raise ValueError(f"[automations.{name}] must be a table, got"
                         f" {type(rule).__name__}")
    for key in rule:
        if key not in AUTOMATION_KEYS[name]:
            accepted = ", ".join(sorted(AUTOMATION_KEYS[name]))
            raise ValueError(f"[automations.{name}] {key}: unknown key;"
                             f" it accepts: {accepted}")
    mode = rule.get("mode", OFF)
    if mode not in MAIN_WATCH_MODES:
        raise ValueError(f"[automations.{name}] mode must be one of"
                         f" {', '.join(MAIN_WATCH_MODES)}, got {mode!r}")
    revision = rule.get("revision")
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError(f"[automations.{name}] revision must be a"
                         f" whole number, got {revision!r}")
    if mode == ACT and revision is None:
        raise ValueError(f'[automations.{name}] mode = "act" needs'
                         " revision, the rule revision approved to act")
    return rule, mode, revision


def main_watch_config(project):
    _rule, mode, revision = rule_table(project, MAIN_WATCH)
    return WatchConfig(mode, revision)


def smoke_config(project):
    rule, mode, revision = rule_table(project, SMOKE)
    command, every = rule.get("command"), rule.get("every_hours")
    if mode != OFF and (not isinstance(command, str) or not command.strip()):
        raise ValueError(f"[automations.{SMOKE}] command must be a non-empty"
                         f" string, got {command!r}")
    if mode != OFF and (type(every) is not int or every < 1):
        raise ValueError(f"[automations.{SMOKE}] every_hours must be a whole"
                         f" number of at least 1, got {every!r}")
    return SmokeConfig(mode, revision, command, every)


def unapproved(config, revision, name=MAIN_WATCH):
    if config.revision == revision:
        return None
    return (f"rule revision {revision} is not approved"
            f" ([automations.{name}] revision = {config.revision})")


def check_automations(project):
    try:
        main_watch_config(project)
        smoke_config(project)
    except ValueError as bad:
        raise SystemExit(f"[holo2] {project.config_path}: {bad}") from None
