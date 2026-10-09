import collections
from pathlib import PurePosixPath

REVIEW_KEYS = {"high_paths": (), "medium_paths": (), "fan_in": 10, "packages": 3,
               "adversary": False}
ReviewConfig = collections.namedtuple("ReviewConfig", REVIEW_KEYS)


def _globs(project, key, value):
    if not isinstance(value, (list, tuple)) or any(
            not isinstance(p, str) or not p.strip()
            or PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts
            for p in value):
        raise SystemExit(
            f"[holo2] {project.config_path}: [review] {key} must be a list of "
            f"non-empty repository-relative globs without `..`, got {value!r}")
    return tuple(value)


def _floor(project, key, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemExit(
            f"[holo2] {project.config_path}: [review] {key} must be an integer "
            f"of at least 0, got {value!r}")
    return value


def _switch(project, key, value):
    if not isinstance(value, bool):
        raise SystemExit(
            f"[holo2] {project.config_path}: [review] {key} must be true or "
            f"false, got {value!r}")
    return value


def review_config(project):
    table = project.config().get("review", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [review] must be a table, got "
            f"{type(table).__name__}")
    values = {key: table.get(key, default) for key, default in REVIEW_KEYS.items()}
    return ReviewConfig(
        high_paths=_globs(project, "high_paths", values["high_paths"]),
        medium_paths=_globs(project, "medium_paths", values["medium_paths"]),
        fan_in=_floor(project, "fan_in", values["fan_in"]),
        packages=_floor(project, "packages", values["packages"]),
        adversary=_switch(project, "adversary", values["adversary"]))
