import json
import re
import subprocess
from pathlib import PurePosixPath
from time import monotonic
from typing import NamedTuple

from holophyte.agents.agent_output import claude_result
from holophyte.agents.agent_turns import argv_label
from holophyte.agents.harness import SCOUT_LEAD
from holophyte.loop.gates import run_capped
from holophyte.redact import known_secrets, outbound
from holophyte.scout.scout_config import scout_config
from holophyte.scout.scout_probes import probe_scout_route
from store.enums import ScoutKind, ScoutTaskMode
from store.scouts import (
    SYNTHESIZING,
    move_scout,
    record_scout_event,
    record_scout_report,
    record_scout_tasks,
    set_scout_session,
)

REPORT_TURN_S = 600
PLAN_OPENING = "Scout plan turn"
REPORT_OPENING = "Scout report turn"
NO_REPORT = "the lead's report turn returned no JSON object"
MODES = tuple(mode.value for mode in ScoutTaskMode)
FENCED_JSON = re.compile(r"^[ \t]*```json[ \t]*\n(.*?)^[ \t]*```",
                         re.DOTALL | re.MULTILINE)
TOKENS_USED = re.compile(r"tokens used\D{0,4}([\d,]+)", re.IGNORECASE)
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


def plan_tasks(answer, limit):
    if not isinstance(answer, dict) or not isinstance(answer.get("tasks"), list):
        return None, "the answer holds no tasks list"
    tasks = answer["tasks"]
    if len(tasks) > limit:
        return None, (f"the plan lists {len(tasks)} tasks, more than "
                      f"helperTurns = {limit}")
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


def report_goal(row, tasks, problem, fresh):
    unrun = "\n".join(f"- [{task['mode']}] {task['question']}"
                      for task in tasks) or "- none: the plan listed no sub-task"
    if problem is not None:
        unrun = f"- none: no plan was stored, because {problem}"
    shape = REPORT_SHAPE + (REPRODUCTION_SHAPE if row.kind == ScoutKind.BUG.value
                            else "") + "}"
    return (
        f"{REPORT_OPENING}\n\n"
        + (f"{asked(row)}\n\n" if fresh else "")
        + "No helper ran. These sub-tasks of the plan stay unanswered:\n"
        f"{unrun}\n\n"
        "Write the scout's report from what you have read: what you found, "
        "which part was cut and why, and the actions you would take. Do not "
        "edit files, commit or push. End your answer with one fenced json "
        "block in this shape, TEXT standing for a string:\n\n"
        f"```json\n{shape}\n```\n")


def turn_answer(seat, output):
    if seat.name == "claude":
        result = claude_result(output)
        return result if result is not None else (output, None)
    counts = TOKENS_USED.findall(output)
    usage = {"total_tokens": int(counts[-1].replace(",", ""))} if counts else None
    return output, usage


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


def stored_plan(conn, scout_id, turn, limit):
    if turn.timed_out:
        tasks, problem = None, "the plan turn was cut at the scout's time budget"
    else:
        tasks, problem = plan_tasks(fenced_json(turn.answer), limit)
    if problem is not None:
        record_scout_event(conn, scout_id, "scout_plan",
                           f"no plan stored: {problem}")
        return [], problem
    record_scout_tasks(conn, scout_id, 1, 1,
                       [{key: value for key, value in task.items()
                         if key != "paths"} for task in tasks])
    return tasks, None


def run_lead(conn, project, row, worktree, budget_s):
    """Returns the phase the scout ends in and the reason it ends there."""
    config = scout_config(project)
    name = row.lead_route or config.lead
    seat, limit = config.leads.get(name), row.helper_turns or config.helper_turns
    if seat is None:
        return "failed", f"lead route {name} is down: no [scout.leads.{name}]"
    probe = probe_scout_route(project, f"{SCOUT_LEAD}:{name}", seat)
    if not probe.ok:
        return "failed", f"lead route {name} is down: {probe.reason()}"
    secrets = known_secrets(project.config())
    argv = seat.turn(outbound(plan_goal(row, limit), secrets))
    session = seat.session(argv)
    if session is not None:
        set_scout_session(conn, row.id, session)
    turn = lead_turn(conn, project, row.id, seat, argv, worktree, budget_s)
    if session is None:
        session = seat.reported_session(turn.output)
        if session is not None:
            set_scout_session(conn, row.id, session)
    tasks, problem = stored_plan(conn, row.id, turn, limit)
    move_scout(conn, row.id, SYNTHESIZING)
    goal = outbound(report_goal(row, tasks, problem, session is None),
                    secrets)
    argv = seat.resume(session) + [goal] if session else seat.turn(goal)
    turn = lead_turn(conn, project, row.id, seat, argv, worktree, REPORT_TURN_S)
    report = fenced_json(turn.answer)
    if not isinstance(report, dict):
        return "failed", NO_REPORT
    record_scout_report(conn, row.id, 1, report)
    return "done", "the lead wrote its report"
