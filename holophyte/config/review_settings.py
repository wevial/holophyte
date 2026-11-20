import collections
from pathlib import PurePosixPath

from holophyte.review.review_runner import EFFORTS

REVIEW_KEYS = {"high_paths": (), "medium_paths": (), "fan_in": 10, "packages": 3,
               "adversary": False, "council": ()}
ReviewConfig = collections.namedtuple("ReviewConfig", REVIEW_KEYS)
CouncilSeat = collections.namedtuple("CouncilSeat", "harness model effort")
COUNCIL_HARNESSES = ("codex", "claude")


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


def _seat(project, number, entry):
    where = f"[holo2] {project.config_path}: [[review.council]] entry {number}"
    if not isinstance(entry, dict) or set(entry) != set(CouncilSeat._fields):
        raise SystemExit(
            f"{where} must be a table with exactly harness, model and effort, "
            f"got {entry!r}")
    if entry["harness"] not in COUNCIL_HARNESSES:
        raise SystemExit(
            f'{where}: harness must be "codex" or "claude", got '
            f"{entry['harness']!r}")
    if not isinstance(entry["model"], str) or not entry["model"].strip():
        raise SystemExit(
            f"{where}: model must be a non-empty string, got {entry['model']!r}")
    if entry["effort"] not in EFFORTS:
        raise SystemExit(
            f"{where}: effort must be one of {', '.join(EFFORTS)}, got "
            f"{entry['effort']!r}")
    return CouncilSeat(entry["harness"], entry["model"], entry["effort"])


def _council(project, value):
    if not isinstance(value, (list, tuple)):
        raise SystemExit(
            f"[holo2] {project.config_path}: [review] council must be "
            f"[[review.council]] tables, got {value!r}")
    if len(value) == 1:
        raise SystemExit(
            f"[holo2] {project.config_path}: [[review.council]] entry 1 is the "
            "only seat; a council needs at least two")
    return tuple(_seat(project, number, entry)
                 for number, entry in enumerate(value, 1))


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
        adversary=_switch(project, "adversary", values["adversary"]),
        council=_council(project, values["council"]))
