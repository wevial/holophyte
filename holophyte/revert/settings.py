import collections

DEFAULT_REFUSE_PATHS = ("**/migrations/**", "**/migrate/**", "**/*.sql",
                        "**/schema.prisma")
REVERT_KEYS = {"enabled": False, "refuse_paths": DEFAULT_REFUSE_PATHS}
RevertConfig = collections.namedtuple("RevertConfig", REVERT_KEYS)


def revert_config(project):
    table = project.config().get("revert", {})
    where = f"[holo2] {project.config_path}: [revert]"
    if not isinstance(table, dict):
        raise SystemExit(f"{where} must be a table, got {type(table).__name__}")
    enabled = table.get("enabled", REVERT_KEYS["enabled"])
    if not isinstance(enabled, bool):
        raise SystemExit(f"{where} enabled must be true or false, got"
                         f" {enabled!r}")
    globs = table.get("refuse_paths", REVERT_KEYS["refuse_paths"])
    if not isinstance(globs, (list, tuple)) or any(
            not isinstance(glob, str) or not glob.strip() for glob in globs):
        raise SystemExit(f"{where} refuse_paths must be a list of non-empty"
                         f" path globs (strings), got {globs!r}")
    return RevertConfig(enabled=enabled, refuse_paths=tuple(globs))
