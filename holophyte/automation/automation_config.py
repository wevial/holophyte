"""`[automations]`: which rules a project opts into, and in what mode."""
from typing import NamedTuple

MAIN_WATCH = "main_watch"
OFF, DRY_RUN, ACT = "off", "dry_run", "act"
MAIN_WATCH_MODES = (OFF, DRY_RUN, ACT)
AUTOMATION_KEYS = {MAIN_WATCH: frozenset({"mode", "revision"})}
BUILT_INS = tuple(AUTOMATION_KEYS)


class WatchConfig(NamedTuple):
    mode: str
    revision: int | None


def main_watch_config(project):
    table = project.config().get("automations", {})
    if not isinstance(table, dict):
        raise ValueError(f"[automations] must be a table, got"
                         f" {type(table).__name__}")
    watch = table.get(MAIN_WATCH, {})
    if not isinstance(watch, dict):
        raise ValueError(f"[automations.{MAIN_WATCH}] must be a table, got"
                         f" {type(watch).__name__}")
    for key in watch:
        if key not in AUTOMATION_KEYS[MAIN_WATCH]:
            raise ValueError(f"[automations.{MAIN_WATCH}] {key}: unknown key;"
                             f" it accepts: mode, revision")
    mode = watch.get("mode", OFF)
    if mode not in MAIN_WATCH_MODES:
        raise ValueError(f"[automations.{MAIN_WATCH}] mode must be one of"
                         f" {', '.join(MAIN_WATCH_MODES)}, got {mode!r}")
    revision = watch.get("revision")
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError(f"[automations.{MAIN_WATCH}] revision must be a"
                         f" whole number, got {revision!r}")
    if mode == ACT and revision is None:
        raise ValueError(f'[automations.{MAIN_WATCH}] mode = "act" needs'
                         " revision, the rule revision approved to act")
    return WatchConfig(mode, revision)


def unapproved(config, revision):
    if config.revision == revision:
        return None
    return (f"rule revision {revision} is not approved"
            f" ([automations.{MAIN_WATCH}] revision = {config.revision})")


def check_automations(project):
    try:
        main_watch_config(project)
    except ValueError as bad:
        raise SystemExit(f"[holo2] {project.config_path}: {bad}") from None
