import json
import re
import subprocess
from pathlib import PurePosixPath
from time import monotonic
from typing import NamedTuple

from holophyte.agents.agent_turns import argv_label
from holophyte.agents.harness import SCOUT_LEAD
from holophyte.loop.gates import run_capped
from holophyte.redact import known_secrets, outbound
from holophyte.scout.scout_config import scout_config
from holophyte.scout.scout_probes import probe_scout_route
from holophyte.scout.swarm import Swarm, turn_answer
from store.enums import ScoutKind, ScoutTaskMode
from store.scouts import (
    SWARMING,
    SYNTHESIZING,
    move_scout,
    record_scout_event,
    record_scout_report,
    set_scout_session,
)

REPORT_TURN_S = 600
PLAN_OPENING = "Scout plan turn"
REPORT_OPENING = "Scout report turn"
SYNTHESIS_OPENING = "Scout synthesis turn"
NO_REPORT = "the lead's report turn returned no JSON object"
MODES = tuple(mode.value for mode in ScoutTaskMode)
FENCED_JSON = re.compile(r"^[ \t]*```json[ \t]*\n(.*?)^[ \t]*```",
                         re.DOTALL | re.MULTILINE)
PLAN_SHAPE = ('{"tasks": [{"question": TEXT, "paths": [PATH, ...], '
              '"mode": "read" or "run"}, ...]}')
REPORT_SHAPE = (
    '{"summary": TEXT,\n'
    ' "findings": [{"title": TEXT, "status": TEXT, "evidence": TEXT}, ...],\n'
    ' "actions": [{"title": TEXT, "why": TEXT, "kind": TEXT, "body": TEXT},'
    ' ...]')
REPRODUCTION_SHAPE = ',\n "reproduction": {"level": TEXT, "hypotheses": [TEXT, ...]}'


class LeadTurn(NamedTuple):
    output: str
    answer: str
    timed_out: bool


def fenced_json(output):
    blocks = FENCED_JSON.findall(output or "")
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except ValueError:
        return None


def task_problem(number, task):
    if not isinstance(task, dict):
        return f"task {number} is not an object"
    question = task.get("question")
    if not isinstance(question, str) or not question.strip():
        return f"task {number}: question must be non-empty text"
    paths = task.get("paths")
    if not isinstance(paths, list) or not all(map(relative_path, paths)):
        return f"task {number}: paths must be a list of repository-relative paths"
    if task.get("mode") not in MODES:
        return f"task {number}: mode must be one of {', '.join(MODES)}"
    return None


def relative_path(path):
    return (isinstance(path, str) and bool(path.strip())
            and not PurePosixPath(path).is_absolute()
            and ".." not in PurePosixPath(path).parts)


def plan_tasks(answer):
    if not isinstance(answer, dict) or not isinstance(answer.get("tasks"), list):
        return None, "the answer holds no tasks list"
    tasks = answer["tasks"]
    for number, task in enumerate(tasks, 1):
        problem = task_problem(number, task)
        if problem is not None:
            return None, problem
    return tasks, None


def asked(row):
    links = "\n".join(f"- {link}" for link in row.links) or "none"
    return (f"Ask: {row.ask}\nKind: {row.kind}\nLinks:\n{links}\n"
            f"Base commit: {row.base_sha}")


def plan_goal(row, limit):
    return (
        f"{PLAN_OPENING}\n\n"
        "You lead a scout: a read-only investigation of this checkout. Do not "
        "edit files, commit or push.\n\n"
        f"{asked(row)}\n\n"
        f"Plan the work as at most {limit} sub-tasks. Each is one question a "
        "helper answers by reading the code (mode \"read\") or by running "
        "commands in its own checkout (mode \"run\"), with the "
        "repository-relative paths it should start from. End your answer "
        "with one fenced json block in this shape, TEXT and PATH standing "
        "for strings:\n\n"
        f"```json\n{PLAN_SHAPE}\n```\n")


def answered_tasks(swarm):
    return "\n\n".join(
        f"### [{swarm.answered(task).phase}] {task.question}\n"
        f"{swarm.answered(task).excerpt.strip() or 'no output'}"
        for task in swarm.tasks)


def synthesis_goal(row, swarm, fresh):
    return (
        f"{SYNTHESIS_OPENING}\n\n"
        + (f"{asked(row)}\n\n" if fresh else "")
        + "Your helpers have answered a wave of the plan. Each sub-task so "
        "far, with its phase and the end of its output:\n\n"
        f"{answered_tasks(swarm)}\n\n"
        "Merge what they found. If one more wave of sub-tasks would settle "
        "what is still open, plan it as at most "
        f"{swarm.total - swarm.launched} sub-tasks; otherwise list none. Do "
        "not edit files, commit or push. End your answer with one fenced "
        "json block in this shape, TEXT and PATH standing for strings:\n\n"
        f"```json\n{PLAN_SHAPE}\n```\n")


def report_goal(row, swarm, problem, fresh):
    answers = answered_tasks(swarm) or "- none: the plan listed no sub-task"
    if problem is not None:
        answers = f"- none: no plan was stored, because {problem}"
    shape = REPORT_SHAPE + (REPRODUCTION_SHAPE if row.kind == ScoutKind.BUG.value
                            else "") + "}"
    return (
        f"{REPORT_OPENING}\n\n"
        + (f"{asked(row)}\n\n" if fresh else "")
        + "The plan's sub-tasks, each with its phase and the end of its "
        "helper's output (a queued one never ran, a stopped one was cut at "
        f"the time budget):\n\n{answers}\n\n"
        "Write the scout's report from what you have read: what you found, "
        "which part was cut and why, and the actions you would take. Do not "
        "edit files, commit or push. End your answer with one fenced json "
        "block in this shape, TEXT standing for a string:\n\n"
        f"```json\n{shape}\n```\n")


def lead_turn(conn, project, scout_id, seat, argv, worktree, cap):
    label = outbound(argv_label(argv), known_secrets(project.config()))
    payload = dict(role=SCOUT_LEAD, label=label, route="primary",
                   exit_status=None, timed_out=False)
    started, output = monotonic(), ""
    try:
        try:
            payload["exit_status"], output = run_capped(argv, worktree, cap)
        except subprocess.TimeoutExpired as expired:
            output = expired.output or ""
            payload["timed_out"] = True
        answer, usage = turn_answer(seat, output)
        if usage is not None:
            payload["usage"] = usage
        return LeadTurn(output, answer, payload["timed_out"])
    finally:
        payload["seconds"] = monotonic() - started
        record_scout_event(conn, scout_id, "agent_turn",
                           f"{SCOUT_LEAD} turn ended", level="detail",
                           payload=json.dumps(payload))


class Lead:
    def __init__(self, conn, project, row, seat, worktree):
        self.conn, self.project, self.row = conn, project, row
        self.seat, self.worktree = seat, worktree
        self.secrets = known_secrets(project.config())
        self.session = None

    def remember(self, session):
        if session is not None:
            self.session = session
            set_scout_session(self.conn, self.row.id, session)

    def turn(self, argv, cap):
        return lead_turn(self.conn, self.project, self.row.id, self.seat, argv,
                         self.worktree, cap)

    def plan(self, limit, cap):
        argv = self.seat.turn(outbound(plan_goal(self.row, limit), self.secrets))
        self.remember(self.seat.session(argv))
        turn = self.turn(argv, cap)
        if self.session is None:
            self.remember(self.seat.reported_session(turn.output))
        return turn

    def resume(self, goal, cap):
        goal = outbound(goal, self.secrets)
        return self.turn(self.seat.resume(self.session) + [goal] if self.session
                         else self.seat.turn(goal), cap)


def wave_tasks(conn, swarm, turn, wave, cut):
    if turn.timed_out:
        tasks, problem = None, f"the {cut} turn was cut at the time budget"
    else:
        tasks, problem = plan_tasks(fenced_json(turn.answer))
    if problem is not None:
        record_scout_event(conn, swarm.row.id, "scout_plan",
                           f"no wave {wave} stored: {problem}")
        return [], problem
    return swarm.plan(wave, tasks), None


def run_waves(conn, lead, swarm, tasks, waves):
    if not (tasks and swarm.ready()):
        move_scout(conn, lead.row.id, SYNTHESIZING)
        return
    for wave in range(1, waves + 1):
        move_scout(conn, lead.row.id, SWARMING)
        swarm.run(tasks)
        move_scout(conn, lead.row.id, SYNTHESIZING)
        if wave == waves or not swarm.left():
            return
        turn = lead.resume(synthesis_goal(lead.row, swarm,
                                          lead.session is None),
                           swarm.remaining_s())
        tasks, _ = wave_tasks(conn, swarm, turn, wave + 1, "synthesis")
        if not tasks:
            return


def run_lead(conn, project, row, worktree, budget_s):
    """Returns the phase the scout ends in and the reason it ends there."""
    deadline = monotonic() + budget_s
    config = scout_config(project)
    name = row.lead_route or config.lead
    seat, limit = config.leads.get(name), row.helper_turns or config.helper_turns
    if seat is None:
        return "failed", f"lead route {name} is down: no [scout.leads.{name}]"
    probe = probe_scout_route(project, f"{SCOUT_LEAD}:{name}", seat)
    if not probe.ok:
        return "failed", f"lead route {name} is down: {probe.reason()}"
    lead = Lead(conn, project, row, seat, worktree)
    turn = lead.plan(limit, budget_s)
    with Swarm(conn, project, row, config, deadline) as swarm:
        tasks, problem = wave_tasks(conn, swarm, turn, 1, "plan")
        run_waves(conn, lead, swarm, tasks, row.waves or config.waves)
        turn = lead.resume(report_goal(row, swarm, problem,
                                       lead.session is None), REPORT_TURN_S)
        report = fenced_json(turn.answer)
        if not isinstance(report, dict):
            return "failed", NO_REPORT
        record_scout_report(conn, row.id, 1, dict(report, cut=swarm.cut()))
    return "done", "the lead wrote its report"
