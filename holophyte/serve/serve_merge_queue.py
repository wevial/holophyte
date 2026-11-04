import fcntl
import json
import os
from time import time
from typing import Literal, NotRequired, TypedDict

import store.read
from holophyte.loop.gates import InfraFailure
from holophyte.pr import github
from holophyte.pr.merge_queue import queue_read
from holophyte.serve.serve_runs import no_store
from store.operate import PARKED_PHASES

MERGE_QUEUE_PATH = "/merge-queue"
CACHE_SEC = 15
CACHE_FILE = "merge-queue.json"


class MergeQueueConfiguration(TypedDict):
    merge_method: str | None
    merging_strategy: str | None
    maximum_entries_to_build: int | None
    minimum_entries_to_merge: int | None
    check_response_timeout: int | None


class MergeQueuePull(TypedDict):
    number: int
    title: str | None
    author: str | None
    url: str
    head_oid: str | None


class MergeQueueRun(TypedDict):
    id: int
    ticket: str
    phase: str
    round: int


class MergeQueueCheck(TypedDict):
    name: str
    workflow: str | None
    status: str
    conclusion: str | None
    started_ms: int | None
    completed_ms: int | None
    url: str | None
    required: bool


class MergeQueueChecks(TypedDict):
    done: int
    total: int
    failed: int
    runs: list[MergeQueueCheck]


class MergeQueueEntry(TypedDict):
    position: int
    state: str
    enqueued_ms: int | None
    estimated_s: int | None
    jump: bool
    solo: bool
    pr: MergeQueuePull
    group_sha: str | None
    building_ms: int | None
    run: MergeQueueRun | None
    checks: MergeQueueChecks


class MergeQueueLeft(TypedDict):
    pr: MergeQueuePull
    at_ms: int
    outcome: Literal["merged", "removed"]
    reason: str | None
    run: MergeQueueRun | None
    waits_on_you: bool


class MergeQueue(TypedDict):
    configured: bool | None
    branch: str
    now: int
    configuration: MergeQueueConfiguration | None
    entries: list[MergeQueueEntry]
    left: list[MergeQueueLeft]
    error: NotRequired[str]


def merge_queue(project, query=None):
    if not project.store_path.exists():
        return 503, no_store(project)
    path = project.holo_dir / CACHE_FILE
    # Taken by every daemon process serving the project: one read for all.
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = _cached(path)
        if cached is not None:
            return 200, cached
        read_at = time()
        answer = read_merge_queue(project)
        _keep(path, read_at, answer)
    return 200, answer


def _cached(path):
    try:
        kept = json.loads(path.read_text())
        age = time() - kept["read_at"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return kept.get("answer") if 0 <= age < CACHE_SEC else None


def _keep(path, read_at, answer):
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps({"read_at": read_at, "answer": answer}))
    os.replace(partial, path)


def read_merge_queue(project):
    now_ms = int(time() * 1000)
    head = {"branch": github.BASE, "now": now_ms}
    try:
        queue = queue_read(project, now_ms)
    except InfraFailure as failure:
        return {"configured": None, **head, "configuration": None,
                "entries": [], "left": [], "error": str(failure)}
    rows = queue["entries"] + queue["left"]
    conn = store.read.open_readonly(project.store_path)
    try:
        runs = store.read.runs_for_prs(conn, [row["pr"]["url"] for row in rows])
    finally:
        conn.close()
    for row in rows:
        run = runs.get(row["pr"]["url"])
        row["run"] = None if run is None else {
            "id": run.id, "ticket": run.ticket, "phase": run.phase,
            "round": run.reviewRoundCount}
    for row in queue["left"]:
        run = runs.get(row["pr"]["url"])
        row["waits_on_you"] = row["outcome"] == "removed" and (
            run is None or run.phase in PARKED_PHASES
            or run.outcome == "failed")
    return {"configured": queue["configured"], **head, **queue}
