import json
import os
import re
import shutil
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import partial
from time import monotonic
from typing import NamedTuple

import store
from holophyte.agents.agent_output import claude_result
from holophyte.agents.agent_turns import argv_label
from holophyte.agents.harness import SCOUT_HELPER
from holophyte.agents.probes import PROBE_TIMEOUT
from holophyte.agents.review_workspace import review_scratch, scratch_git_environment
from holophyte.isolation import launcher
from holophyte.loop.gates import reap_group, run_capped, sh
from holophyte.redact import known_secrets, outbound, redact_prose
from holophyte.scout.run_helpers import (
    HELPER_OPENING,
    LAUNCH_FAILURES,
    READ_ONLY,
    audited,
    isolated_probe,
    launch_turn,
    run_clone,
    run_goal,
    run_seat,
    runs,
    setup_failure,
    task_fields,
)
from holophyte.scout.scout_probes import in_critic_workspace, probe_scout_route
from holophyte.scout.telemetry import turn_cost
from store.enums import ScoutKind, ScoutTaskPhase
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


def turn_answer(seat, output, price):
    answer, usage = reported_answer(seat, output)
    if usage is not None:
        usage["cost_usd"] = turn_cost(price, usage_tokens(usage),
                                      usage.get("cost_usd"))
    return answer, usage


def reported_answer(seat, output):
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


class ScoutStopped(BaseException):
    pass


def kill_group(proc):
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Stopping:
    def __init__(self):
        self.lock, self.procs, self.stopped = threading.RLock(), set(), False
        self.starting = False

    def run_capped(self, argv, cwd, cap, on_start=None):
        return self.tracked(partial(run_capped, argv, cwd, cap), on_start)

    def tracked(self, run, on_start=None):
        mine, main = [], threading.current_thread() is threading.main_thread()

        def start(proc):
            with self.lock:
                mine.append(proc)
                self.procs.add(proc)
                if self.stopped:
                    kill_group(proc)
            if main:
                self.started()
            if on_start is not None:
                on_start(proc)

        if main:
            self.starting = True
        try:
            return run(on_start=start)
        finally:
            with self.lock:
                self.procs.difference_update(mine)
            if main and self.starting:
                self.started()

    def started(self):
        self.starting = False
        self.check()

    def stop(self):
        with self.lock:
            first, self.stopped = not self.stopped, True
            for proc in list(self.procs):
                kill_group(proc)
        return first

    def check(self):
        if self.stopped:
            raise ScoutStopped()


def capped_turn(turn, stopping, failures=(OSError,)):
    try:
        code, output = turn()
    except subprocess.TimeoutExpired as expired:
        return HelperTurn(STOPPED, None, expired.output or "")
    except failures as failed:
        return HelperTurn(FAILED, None, str(failed))
    if code != 0 and stopping.stopped:
        return HelperTurn(STOPPED, code, output or "")
    return HelperTurn(DONE if code == 0 else FAILED, code, output or "")


class Swarm:
    """Runs a scout's helper turns, one wave at a time, until it is closed."""

    def __init__(self, conn, project, row, config, deadline, stopping,
                 pass_number=1, total=None):
        self.conn, self.project, self.row = conn, project, row
        self.deadline = deadline
        self.stopping = stopping
        self.pass_number = pass_number
        self.route = row.helper_route or config.helper
        self.seat = config.helpers.get(self.route)
        self.price = config.helper_prices.get(self.route)
        self.total = total or row.helper_turns or config.helper_turns
        self.size = min(row.swarm or config.swarm, self.total)
        self.secrets = known_secrets(project.config())
        (self.store_path,) = [path for _, name, path in
                              conn.execute("PRAGMA database_list")
                              if name == "main"]
        self.launched, self.tasks, self.answers = 0, [], {}
        self.scratch = None
        self.probing, self.downs = threading.Lock(), {}
        self.stack = ExitStack()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stack.close()

    def remaining_s(self):
        return max(0.0, self.deadline - monotonic())

    def left(self):
        return (self.launched < self.total and self.remaining_s() > 0
                and not self.stranded())

    def ready(self):
        if self.seat is not None:
            return True
        record_scout_event(self.conn, self.row.id, "scout_helpers",
                           f"helper route {self.route} is down: "
                           f"no [scout.helpers.{self.route}]")
        return False

    def contained(self):
        return launcher.route_for(self.project).backend == "container"

    def stranded(self):
        if not self.downs.get("host"):
            return False
        return (not (self.row.kind == ScoutKind.BUG.value and self.contained())
                or bool(self.downs.get("container")))

    def host_probe(self):
        probe = probe_scout_route(
            self.project, f"{SCOUT_HELPER}:{self.route}", self.seat,
            min(PROBE_TIMEOUT, self.remaining_s()),
            partial(in_critic_workspace, run=self.stopping.run_capped))
        return None if probe.ok else (
            f"helper route {self.route} is down: {probe.reason()}")

    def route_down(self, conn, key, probe):
        with self.probing:
            if key not in self.downs:
                down = probe()
                if down is not None and self.stopping.stopped:
                    return down
                self.downs[key] = down
                if self.downs[key] is not None:
                    record_scout_event(conn, self.row.id, "scout_helpers",
                                       self.downs[key])
            return self.downs[key]

    def plan(self, wave, tasks):
        ids = record_scout_tasks(
            self.conn, self.row.id, self.pass_number, wave,
            [{"question": task["question"], "mode": task["mode"],
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
        if self.remaining_s() <= 0 or self.stopping.stopped:
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
            if self.route_down(conn, "host", self.host_probe) is not None:
                return
            prepared, cap = self.prepared(task), self.remaining_s()
            if prepared is None or cap <= 0:
                return
            argv, checkout = prepared
            turn = capped_turn(lambda: self.stopping.run_capped(
                argv, checkout, cap, start), self.stopping)
            if started:
                self.record(conn, task, argv, turn, monotonic() - started[0])
        finally:
            mine.set()
            if conn is not None:
                conn.close()

    def run_helper(self, conn, task, mine):
        if self.remaining_s() <= 0 or self.stopping.stopped:
            return
        try:
            checkout = run_clone(self.project, self.row.id, task.id,
                                 self.row.base_sha)
        except (RuntimeError, OSError) as error:
            start_scout_task(conn, task.id)
            mine.set()
            self.end(conn, task, FAILED,
                     redact_prose(str(error), self.secrets)[:EXCERPT_CHARS])
            return
        try:
            self.run_in(conn, task, mine, checkout)
        finally:
            shutil.rmtree(checkout, ignore_errors=True)

    def run_in(self, conn, task, mine, checkout):
        start_scout_task(conn, task.id)
        started = monotonic()
        mine.set()
        seat = run_seat(self.project, self.seat)
        problem = (setup_failure(self.project, checkout, self.deadline,
                                 self.stopping.tracked)
                   or self.isolated_down(conn, seat, checkout))
        if problem is not None and self.stopping.stopped:
            self.end(conn, task, STOPPED, "")
            return
        if problem is not None:
            self.end(conn, task, FAILED,
                     redact_prose(problem, self.secrets)[:EXCERPT_CHARS])
            return
        argv = seat.turn(outbound(run_goal(self.row, task), self.secrets))
        cap = self.remaining_s()
        around = partial(audited, conn, self.row.id)
        launch = partial(launch_turn, self.project, argv, checkout, cap, around)
        turn = (capped_turn(lambda: self.stopping.tracked(launch),
                            self.stopping, LAUNCH_FAILURES)
                if cap > 0 else HelperTurn(STOPPED, None, ""))
        self.record(conn, task, argv, turn, monotonic() - started)

    def isolated_down(self, conn, seat, checkout):
        if not self.contained():
            return self.route_down(conn, "host", self.host_probe)
        return self.route_down(conn, "container", partial(
            isolated_probe, self.project, self.route, seat, checkout,
            self.remaining_s(), self.stopping.tracked))

    def refuse_push(self, checkout):
        from holophyte.scout.scout_process import refuse_push

        refuse_push(self.project.path, checkout, self.scratch / "hooks",
                    scratch_git_environment())

    def end(self, conn, task, phase, excerpt, usage=None):
        end_scout_task(conn, task.id, phase, tokens=usage_tokens(usage),
                       cost_usd=(usage or {}).get("cost_usd"), excerpt=excerpt)
        self.answers[task.id] = Answered(phase, excerpt)

    def record(self, conn, task, argv, turn, seconds):
        answer, usage = turn_answer(self.seat, turn.output, self.price)
        excerpt = redact_prose(answer, self.secrets)[-EXCERPT_CHARS:]
        self.end(conn, task, turn.phase, excerpt, usage)
        payload = dict(role=SCOUT_HELPER,
                       label=outbound(argv_label(argv), self.secrets),
                       route=self.route, exit_status=turn.exit_status,
                       timed_out=turn.phase == STOPPED and turn.exit_status is None,
                       seconds=seconds)
        if usage is not None:
            payload["usage"] = usage
        record_scout_event(conn, self.row.id, "agent_turn",
                           f"{SCOUT_HELPER} turn ended", level="detail",
                           payload=json.dumps(payload))
