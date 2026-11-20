"""`GET /pipeline`: the pipeline the config resolves now, with stage numbers."""
from __future__ import annotations

import dataclasses
import re
from time import time
from urllib.parse import parse_qs

import store.read
from holophyte.admission import project_of
from holophyte.config.config_tables import board_config
from holophyte.serve.serve_runs import current_secrets
from holophyte.stages import standard
from store.pipelines import pipeline_revision
from store.project_paths import canonical_projects
from store.stages import stage_numbers

PIPELINE_PATH = "/pipeline"
DEFAULT_DAYS = 30
MAX_DAYS = 365
DAY_MS = 24 * 60 * 60 * 1000
SETTING = re.compile(r"\[[A-Za-z0-9_.-]+\]( [A-Za-z0-9_]+)?")


def pipeline(project, query):
    days = parse_qs(query, keep_blank_values=True).get(
        "days", [str(DEFAULT_DAYS)])[-1]
    if not re.fullmatch(r"\d{1,3}", days) or not 1 <= int(days) <= MAX_DAYS:
        return 400, {"error": f"days takes a whole number from 1 to"
                              f" {MAX_DAYS}, not {days!r}"}
    days = int(days)
    secrets = current_secrets(project)
    if secrets is None:
        return 503, {"error": "the project config cannot be read, so its"
                              " pipeline cannot be resolved"}
    fresh = dataclasses.replace(project, _config=None)
    try:
        document = standard.resolve(fresh)
        board = board_config(fresh)
    except (SystemExit, ValueError) as error:
        return 503, {"error": "the project config does not resolve to a"
                              f" pipeline{setting_without_value(error)}"}
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


def served_project(conn, project, board):
    if board is None:
        return project_of(conn, project)
    row = conn.execute("SELECT id FROM projects WHERE linearTeamId = ?",
                       (board.team,)).fetchone()
    if row is None or canonical_projects(conn).get(row[0]) != str(
            project.path.resolve()):
        return None
    return row[0]


def setting_without_value(error):
    found = SETTING.search(str(error).removeprefix("[holo2] "))
    return "" if found is None else f": check {found.group(0)}"
