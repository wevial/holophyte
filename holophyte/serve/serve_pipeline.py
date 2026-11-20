"""`GET /pipeline`: the config's pipeline now, its stage numbers and prompts."""
from __future__ import annotations

import dataclasses
import re
import threading
from time import time
from urllib.parse import parse_qs

import store.read
from holophyte.admission import project_of
from holophyte.config.config_tables import board_config
from holophyte.config.pipeline_settings import NOTE_TARGETS
from holophyte.redact import values_held
from holophyte.serve.serve_runs import current_secrets
from holophyte.stages import previews, standard
from store.pipelines import pipeline_revision
from store.project_paths import canonical_projects
from store.stages import stage_numbers

PIPELINE_PATH = "/pipeline"
PROMPT_PATH = re.compile(r"^/pipeline/prompts/([^/]+)$")
DEFAULT_DAYS = 30
MAX_DAYS = 365
DAY_MS = 24 * 60 * 60 * 1000
SETTING = re.compile(r"\[([A-Za-z0-9_.-]+)\](?: ([A-Za-z0-9_]+))?")
RESOLVING = threading.Lock()


def pipeline(project, query):
    days = parse_qs(query, keep_blank_values=True).get(
        "days", [str(DEFAULT_DAYS)])[-1]
    if not re.fullmatch(r"\d{1,3}", days) or not 1 <= int(days) <= MAX_DAYS:
        return 400, {"error": f"days takes a whole number from 1 to"
                              f" {MAX_DAYS}, not {days!r}"}
    return config_held(project, lambda fresh, secrets: answered(
        project, fresh, int(days), secrets))


def pipeline_prompt(project, target):
    if target not in previews.RENDERS:
        return 404, {"error": "no prompt takes notes by that name; the"
                              " [pipeline.notes] keys are"
                              f" {', '.join(NOTE_TARGETS)}"}
    return config_held(project, lambda fresh, secrets: prompt_answered(
        fresh, target, secrets))


def config_held(project, answer):
    fresh = dataclasses.replace(project, _config=None)
    try:
        own = own_patterns(fresh)
    except (SystemExit, OSError, ValueError):
        own = None
    with RESOLVING, values_held(own or ()):
        secrets = (None if own is None
                   else current_secrets(project, fresh.config()))
        if secrets is None:
            return 503, {"error": "the project config cannot be read, so its"
                                  " pipeline cannot be resolved"}
        return answer(fresh, secrets)


def prompt_answered(fresh, target, secrets):
    try:
        body = previews.preview(fresh, target, lambda text: standard.scrubbed(
            text, secrets))
    except (SystemExit, ValueError) as error:
        named = standard.scrubbed(setting_named(fresh, error), secrets)
        return 503, {"error": "the project config does not resolve to a"
                              f" prompt{named}"}
    return 200, body


def answered(project, fresh, days, secrets):
    try:
        document = standard.resolve(fresh)
        board = board_config(fresh)
    except (SystemExit, ValueError) as error:
        named = standard.scrubbed(setting_named(fresh, error), secrets)
        return 503, {"error": "the project config does not resolve to a"
                              f" pipeline{named}"}
    since_ms = int(time() * 1000) - days * DAY_MS
    revision, numbers = None, {}
    if project.store_path.exists():
        conn = store.read.open_readonly(project.store_path)
        try:
            project_id = served_project(conn, project, board)
            revision = pipeline_revision(conn, project_id, document)
            numbers = stage_numbers(conn, project_id, since_ms, {
                stage["id"] for stage in document["stages"]
                if stage["type"] == "build"})
        finally:
            conn.close()
    held = [stage["id"] for stage in document["stages"]]
    ids = held + sorted(numbers.keys() - set(held))
    stages = [{"id": stage_id, "in_pipeline": stage_id in held,
               **numbers[stage_id]} for stage_id in ids if stage_id in numbers]
    body = {"pipeline": {**document, "revision": revision},
            "window": {"days": days, "since_ms": since_ms}, "stages": stages}
    return 200, standard.scrubbed(body, secrets)


def own_patterns(project):
    merge = project.config().get("merge", {})
    patterns = merge.get("private_patterns", ()) if isinstance(
        merge, dict) else ()
    if not isinstance(patterns, (list, tuple)):
        return []
    return [pattern for pattern in patterns if isinstance(pattern, str)]


def served_project(conn, project, board):
    if board is None:
        return project_of(conn, project)
    row = conn.execute("SELECT id FROM projects WHERE linearTeamId = ?",
                       (board.team,)).fetchone()
    if row is None or canonical_projects(conn).get(row[0]) != str(
            project.path.resolve()):
        return None
    return row[0]


def setting_named(project, error):
    text = str(error).replace(str(project.config_path), "")
    try:
        config = project.config()
    except SystemExit:
        return ""
    for found in SETTING.finditer(text):
        table, key = found.groups()
        value = config
        for part in table.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            continue
        held = isinstance(value, dict) and key in value
        return f": check [{table}]" + (f" {key}" if held else "")
    return ""
