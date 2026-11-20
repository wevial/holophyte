"""`GET /pipeline`: the pipeline the config resolves now, with stage numbers."""
from __future__ import annotations

import dataclasses
import re
from time import time
from urllib.parse import parse_qs

import store.read
from holophyte.admission import project_of
from holophyte.serve.serve_runs import current_secrets
from holophyte.stages import standard
from store.pipelines import pipeline_revision
from store.stages import stage_numbers

PIPELINE_PATH = "/pipeline"
DEFAULT_DAYS = 30
MAX_DAYS = 365
DAY_MS = 24 * 60 * 60 * 1000


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
    document = standard.resolve(dataclasses.replace(project, _config=None))
    since_ms = int(time() * 1000) - days * DAY_MS
    revision, numbers = None, {}
    if project.store_path.exists():
        conn = store.read.open_readonly(project.store_path)
        try:
            revision = pipeline_revision(conn, project_of(conn, project),
                                         document)
            numbers = stage_numbers(conn, since_ms, {
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
