import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from time import monotonic
from typing import NamedTuple

import store
from holophyte.agents.agent_output import claude_result
from holophyte.agents.agent_turns import argv_label
from holophyte.agents.harness import SCOUT_HELPER
from holophyte.agents.review_workspace import review_scratch, scratch_git_environment
from holophyte.loop.gates import reap_group, run_capped, sh
from holophyte.redact import known_secrets, outbound, redact_prose
from holophyte.scout.scout_probes import probe_scout_route
from store.enums import ScoutTaskPhase
from store.scouts import (
    end_scout_task,
    record_scout_event,
    record_scout_tasks,
    start_scout_task,
)

HELPER_OPENING = "Scout helper turn"
EXCERPT_CHARS = 4000
QUEUED, DONE = ScoutTaskPhase.QUEUED.value, ScoutTaskPhase.DONE.value
FAILED, STOPPED = ScoutTaskPhase.FAILED.value, ScoutTaskPhase.STOPPED.value
TOKENS_USED = re.compile(r"tokens used\D{0,4}([\d,]+)", re.IGNORECASE)


class Planned(NamedTuple):
    id: int
    question: str
    paths: list
    mode: str


class Answered(NamedTuple):
    phase: str
    excerpt: str


class HelperTurn(NamedTuple):
    phase: str
    exit_status: int | None
    output: str


def turn_answer(seat, output):
    if seat.name == "claude":
        result = claude_result(output)
        return result if result is not None else (output, None)
    counts = TOKENS_USED.findall(output)
    usage = {"total_tokens": int(counts[-1].replace(",", ""))} if counts else None
    return output, usage


def usage_tokens(usage):
    if usage is None:
        return None
    if "total_tokens" in usage:
        return usage["total_tokens"]
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens")]
    counts = [count for count in counts if isinstance(count, int)]
    return sum(counts) if counts else None


def helper_goal(row, task):
    paths = ", ".join(task.paths) or "none"
    return (
        f"{HELPER_OPENING}\n\n"
        "You help a scout: a read-only investigation of this checkout. Read "
        "and search only: do not edit files, commit or push.\n\n"
        f"Ask: {row.ask}\nKind: {row.kind}\nBase commit: {row.base_sha}\n\n"
        f"Question: {task.question}\nPaths: {paths}\nMode: {task.mode}\n\n"
        "Answer the question with evidence: path:line at the base commit, "
        "commands you ran with excerpts of their output, or links.\n")


def capped_turn(argv, checkout, cap, on_start):
    try:
        code, output = run_capped(argv, checkout, cap, on_start=on_start)
    except subprocess.TimeoutExpired as expired:
        return HelperTurn(STOPPED, None, expired.output or "")
    except OSError as failed:
        return HelperTurn(FAILED, None, str(failed))
    return HelperTurn(DONE if code == 0 else FAILED, code, output or "")


class Swarm:
    """Runs a scout's helper turns, one wave at a time, until it is closed."""

    def __init__(self, conn, project, row, config, deadline):
        self.conn, self.project, self.row = conn, project, row
        self.deadline = deadline
        self.route = row.helper_route or config.helper
        self.seat = config.helpers.get(self.route)
        self.size = row.swarm or config.swarm
        self.total = row.helper_turns or config.helper_turns
        self.secrets = known_secrets(project.config())
        (self.store_path,) = [path for _, name, path in
                              conn.execute("PRAGMA database_list")
                              if name == "main"]
        self.launched, self.tasks, self.answers = 0, [], {}
        self.scratch = None
        self.stack = ExitStack()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stack.close()

    def remaining_s(self):
        return max(0.0, self.deadline - monotonic())

    def left(self):
        return self.launched < self.total and self.remaining_s() > 0

    def ready(self):
        if self.seat is None:
            why = f"no [scout.helpers.{self.route}]"
        else:
            probe = probe_scout_route(self.project,
                                      f"{SCOUT_HELPER}:{self.route}", self.seat)
            if probe.ok:
                return True
            why = probe.reason()
        record_scout_event(self.conn, self.row.id, "scout_helpers",
                           f"helper route {self.route} is down: {why}")
        return False

    def plan(self, wave, tasks):
        ids = record_scout_tasks(self.conn, self.row.id, 1, wave, [
            {"question": task["question"], "mode": task["mode"],
             "route": self.route} for task in tasks])
        planned = [Planned(task_id, task["question"], list(task["paths"]),
                           task["mode"]) for task_id, task in zip(ids, tasks)]
        self.tasks.extend(planned)
        return planned

    def answered(self, task):
        return self.answers.get(task.id, Answered(QUEUED, ""))

    def cut(self):
        return [task.question for task in self.tasks
                if self.answered(task).phase in (QUEUED, STOPPED)]

    def run(self, tasks):
        launch = tasks[:max(0, self.total - self.launched)]
        self.launched += len(launch)
        if self.scratch is None:
            self.scratch = self.stack.enter_context(
                review_scratch(self.project.path))
        starts = [threading.Event() for _ in launch]
        with ThreadPoolExecutor(max_workers=self.size) as pool:
            list(pool.map(self.helper, launch, [None] + starts[:-1], starts))

    def checkout(self, task):
        from holophyte.scout.scout_process import refuse_push

        repo, path = self.project.path, self.scratch / f"helper-{task.id}"
        env = scratch_git_environment()
        sh(["git", "worktree", "add", "--detach", "--quiet", str(path),
            self.row.base_sha], cwd=repo, env=env)
        refuse_push(repo, path, self.scratch / "hooks", env)
        return path

    def prepared(self, task):
        if self.remaining_s() <= 0:
            return None
        checkout = self.checkout(task)
        return self.seat.turn(outbound(helper_goal(self.row, task),
                                       self.secrets)), checkout

    def helper(self, task, previous, mine):
        conn, started = store.open(self.store_path), []

        def start(proc):
            try:
                start_scout_task(conn, task.id)
            except BaseException:
                reap_group(proc, subprocess.TimeoutExpired(proc.args, 0))
                raise
            started.append(monotonic())
            mine.set()

        try:
            if previous is not None:
                previous.wait()
            prepared, cap = self.prepared(task), self.remaining_s()
            if prepared is None or cap <= 0:
                return
            argv, checkout = prepared
            turn = capped_turn(argv, checkout, cap, start)
            if started:
                self.record(conn, task, argv, turn, monotonic() - started[0])
        finally:
            mine.set()
            conn.close()

    def record(self, conn, task, argv, turn, seconds):
        _, usage = turn_answer(self.seat, turn.output)
        excerpt = redact_prose(turn.output, self.secrets)[-EXCERPT_CHARS:]
        end_scout_task(conn, task.id, turn.phase, tokens=usage_tokens(usage),
                       cost_usd=(usage or {}).get("cost_usd"), excerpt=excerpt)
        self.answers[task.id] = Answered(turn.phase, excerpt)
        payload = dict(role=SCOUT_HELPER,
                       label=outbound(argv_label(argv), self.secrets),
                       route=self.route, exit_status=turn.exit_status,
                       timed_out=turn.phase == STOPPED, seconds=seconds)
        if usage is not None:
            payload["usage"] = usage
        record_scout_event(conn, self.row.id, "agent_turn",
                           f"{SCOUT_HELPER} turn ended", level="detail",
                           payload=json.dumps(payload))
