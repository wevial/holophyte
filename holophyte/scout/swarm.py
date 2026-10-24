import json
import re
import shutil
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
from holophyte.agents.probes import PROBE_TIMEOUT
from holophyte.agents.review_workspace import review_scratch, scratch_git_environment
from holophyte.loop.gates import reap_group, run_capped, sh
from holophyte.redact import known_secrets, outbound, redact_prose
from holophyte.scout.run_helpers import (
    HELPER_OPENING,
    LAUNCH_FAILURES,
    READ_ONLY,
    clone_refs,
    isolated_probe,
    launch_turn,
    record_refs,
    run_clone,
    run_goal,
    run_seat,
    runs,
    set_up,
    task_fields,
)
from holophyte.scout.scout_probes import probe_scout_route
from store.enums import ScoutTaskPhase
from store.scouts import (
    end_scout_task,
    record_scout_event,
    record_scout_tasks,
    start_scout_task,
)

EXCERPT_CHARS = 4000
QUEUED, DONE = ScoutTaskPhase.QUEUED.value, ScoutTaskPhase.DONE.value
FAILED, STOPPED = ScoutTaskPhase.FAILED.value, ScoutTaskPhase.STOPPED.value
TOKENS_USED = re.compile(r"tokens used\D{0,4}(\d[\d,]*)", re.IGNORECASE)


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
    return (
        f"{HELPER_OPENING}\n\n{READ_ONLY}\n\n{task_fields(row, task)}"
        "Answer the question with evidence: path:line at the base commit, "
        "commands you ran with excerpts of their output, or links.\n")


def capped_turn(turn, failures=(OSError,)):
    try:
        code, output = turn()
    except subprocess.TimeoutExpired as expired:
        return HelperTurn(STOPPED, None, expired.output or "")
    except failures as failed:
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
        self.probing, self.isolated = threading.Lock(), {}
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
            probe = probe_scout_route(
                self.project, f"{SCOUT_HELPER}:{self.route}", self.seat,
                min(PROBE_TIMEOUT, self.remaining_s()))
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
        path = self.scratch / f"helper-{task.id}"
        sh(["git", "worktree", "add", "--detach", "--quiet", str(path),
            self.row.base_sha], cwd=self.project.path,
           env=scratch_git_environment())
        self.refuse_push(path)
        return path

    def prepared(self, task):
        if self.remaining_s() <= 0:
            return None
        checkout = self.checkout(task)
        return self.seat.turn(outbound(helper_goal(self.row, task),
                                       self.secrets)), checkout

    def helper(self, task, previous, mine):
        conn, started = None, []

        def start(proc):
            try:
                start_scout_task(conn, task.id)
            except BaseException:
                reap_group(proc, subprocess.TimeoutExpired(proc.args, 0))
                raise
            started.append(monotonic())
            mine.set()

        try:
            conn = store.open(self.store_path)
            if previous is not None:
                previous.wait()
            if runs(self.row, task):
                self.run_helper(conn, task, mine)
                return
            prepared, cap = self.prepared(task), self.remaining_s()
            if prepared is None or cap <= 0:
                return
            argv, checkout = prepared
            turn = capped_turn(lambda: run_capped(argv, checkout, cap,
                                                  on_start=start))
            if started:
                self.record(conn, task, argv, turn, monotonic() - started[0])
        finally:
            mine.set()
            if conn is not None:
                conn.close()

    def run_helper(self, conn, task, mine):
        if self.remaining_s() <= 0:
            return
        checkout = run_clone(self.project, self.row.id, task.id,
                             self.row.base_sha)
        try:
            self.run_in(conn, task, mine, checkout)
        finally:
            shutil.rmtree(checkout, ignore_errors=True)

    def run_in(self, conn, task, mine, checkout):
        start_scout_task(conn, task.id)
        started = monotonic()
        mine.set()
        problem = set_up(self.project, checkout, self.deadline)
        if problem is not None:
            self.end(conn, task, FAILED,
                     redact_prose(problem, self.secrets)[:EXCERPT_CHARS])
            return
        seat = run_seat(self.project, self.seat)
        down = self.isolated_down(conn, seat, checkout)
        if down is not None:
            self.end(conn, task, FAILED, down)
            return
        argv = seat.turn(outbound(run_goal(self.row, task), self.secrets))
        cap = self.remaining_s()
        before = clone_refs(checkout)
        try:
            turn = (capped_turn(lambda: launch_turn(self.project, argv,
                                                    checkout, cap),
                                LAUNCH_FAILURES)
                    if cap > 0 else HelperTurn(STOPPED, None, ""))
        finally:
            record_refs(conn, self.row.id, before, clone_refs(checkout))
        self.record(conn, task, argv, turn, monotonic() - started)

    def isolated_down(self, conn, seat, checkout):
        with self.probing:
            if "down" not in self.isolated:
                down = isolated_probe(self.project, self.route, seat,
                                      checkout, self.remaining_s())
                self.isolated["down"] = down
                if down is not None:
                    record_scout_event(conn, self.row.id, "scout_helpers",
                                       down)
            return self.isolated["down"]

    def refuse_push(self, checkout):
        from holophyte.scout.scout_process import refuse_push

        refuse_push(self.project.path, checkout, self.scratch / "hooks",
                    scratch_git_environment())

    def end(self, conn, task, phase, excerpt, usage=None):
        end_scout_task(conn, task.id, phase, tokens=usage_tokens(usage),
                       cost_usd=(usage or {}).get("cost_usd"), excerpt=excerpt)
        self.answers[task.id] = Answered(phase, excerpt)

    def record(self, conn, task, argv, turn, seconds):
        answer, usage = turn_answer(self.seat, turn.output)
        excerpt = redact_prose(answer, self.secrets)[-EXCERPT_CHARS:]
        self.end(conn, task, turn.phase, excerpt, usage)
        payload = dict(role=SCOUT_HELPER,
                       label=outbound(argv_label(argv), self.secrets),
                       route=self.route, exit_status=turn.exit_status,
                       timed_out=turn.phase == STOPPED, seconds=seconds)
        if usage is not None:
            payload["usage"] = usage
        record_scout_event(conn, self.row.id, "agent_turn",
                           f"{SCOUT_HELPER} turn ended", level="detail",
                           payload=json.dumps(payload))
