"""`[automations]`: which rules a project opts into, and in what mode."""
MAIN_WATCH = "main_watch"
OFF, DRY_RUN = "off", "dry_run"
MAIN_WATCH_MODES = (OFF, DRY_RUN)
AUTOMATION_KEYS = {MAIN_WATCH: frozenset({"mode"})}
BUILT_INS = tuple(AUTOMATION_KEYS)


def main_watch_mode(project):
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
                             f" it accepts: mode")
    mode = watch.get("mode", OFF)
    if mode not in MAIN_WATCH_MODES:
        raise ValueError(f"[automations.{MAIN_WATCH}] mode must be one of"
                         f" {', '.join(MAIN_WATCH_MODES)}, got {mode!r}")
    return mode


def check_automations(project):
    try:
        main_watch_mode(project)
    except ValueError as bad:
        raise SystemExit(f"[holo2] {project.config_path}: {bad}") from None
