import json
import re
import subprocess
from functools import partial
from pathlib import PurePosixPath
from time import monotonic
from typing import NamedTuple

import store
from holophyte.agents.agent_turns import argv_label
from holophyte.agents.harness import SCOUT_LEAD
from holophyte.agents.probes import PROBE_TIMEOUT
from holophyte.redact import known_secrets, outbound
from holophyte.scout.context import context_pack
from holophyte.scout.prior import (
    offered_findings,
    prior_reports,
    prior_section,
    scout_paths,
)
from holophyte.scout.report import (
    PRIOR,
    REPRODUCTION,
    REVISE,
    fields_shape,
    offered_fields,
    offers_revise,
    report_problems,
    resolve_evidence,
    ticket_skeleton,
)
from holophyte.scout.run_helpers import reproduced
from holophyte.scout.scout_config import scout_config
from holophyte.scout.scout_probes import in_critic_workspace, probe_scout_route
from holophyte.scout.swarm import Swarm, turn_answer
from store.enums import ScoutKind, ScoutTaskMode
from store.scouts import (
    SWARMING,
    SYNTHESIZING,
    move_scout,
    record_scout_actions,
    record_scout_event,
    record_scout_report,
    set_scout_session,
)
from ticket_template import MAX_ESTIMATE_MIN

REPORT_TURN_S = 600
PLAN_OPENING = "Scout plan turn"
REPORT_OPENING = "Scout report turn"
REPAIR_OPENING = "Scout report repair turn"
SYNTHESIS_OPENING = "Scout synthesis turn"
NO_REPORT = "the lead's report turn returned no JSON object"
MALFORMED = "the lead's report is malformed"
CONTEXT_SLOT = "\x00context\x00"
MODES = tuple(mode.value for mode in ScoutTaskMode)
FENCED_JSON = re.compile(r"^[ \t]*```json[ \t]*\n(.*?)^[ \t]*```",
                         re.DOTALL | re.MULTILINE)
PLAN_SHAPE = ('{"tasks": [{"question": TEXT, "paths": [PATH, ...], '
              '"mode": "read" or "run"}, ...]}')
REPRODUCTION_SHAPE = ",\n" + fields_shape({"reproduction": REPRODUCTION})
PRIOR_SHAPE = ",\n" + fields_shape({"prior": PRIOR})


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


def plan_goal(row, limit, context, prior=""):
    return (
        f"{PLAN_OPENING}\n\n"
        "You lead a scout: a read-only investigation of this checkout. Do not "
        "edit files, commit or push.\n\n"
        f"{asked(row)}\n\n{CONTEXT_SLOT if context else ''}{prior}"
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


def subject_of(row):
    return (None if row.subject_kind is None
            else (row.subject_kind, row.subject_ref))


def report_shape(row, offered=None):
    return ("{" + fields_shape(offered_fields(subject_of(row))).lstrip()
            + (REPRODUCTION_SHAPE if row.kind == ScoutKind.BUG.value else "")
            + (PRIOR_SHAPE if offered else "") + "}")


def revise_rule(row):
    if not offers_revise(subject_of(row)):
        return ""
    return (
        f"This scout is about a ticket defect, so one action may be of kind "
        f"\"{REVISE}\": at most one {REVISE} item, whose body is the defect "
        "ticket's whole body in the skeleton's sections, rewritten so the "
        "stop cannot recur and opening with its \"# \" title line, and whose "
        "why names the wording that caused the stop.\n\n")


def report_ending(row, offered=None):
    return (
        "Write each action's body as a ticket draft in this skeleton, "
        "replacing each <...> with the ticket's text: estimate is whole "
        f"minutes, at most {MAX_ESTIMATE_MIN}, and depends_on lists the "
        "ticket keys it waits on.\n\n"
        f"{ticket_skeleton()}\n\n" + revise_rule(row) +
        "Do not edit files, commit or push. End your answer with one fenced "
        "json block in this shape, TEXT, PATH and URL standing for strings "
        "and N for a line number from 1:\n\n"
        f"```json\n{report_shape(row, offered)}\n```\n")


def report_goal(row, swarm, problem, fresh, scope="", context="",
                offered=None):
    answers = answered_tasks(swarm) or "- none: the plan listed no sub-task"
    if problem is not None:
        answers = f"- none: no plan was stored, because {problem}"
    return (
        f"{REPORT_OPENING}\n\n"
        + (f"{asked(row)}\n\n{CONTEXT_SLOT if context else ''}" if fresh
           else "") + scope
        + "The plan's sub-tasks, each with its phase and the end of its "
        "helper's output (a queued one never ran, a stopped one was cut at "
        f"the time budget):\n\n{answers}\n\n"
        "Write the scout's report from what you have read: what you found, "
        "which part was cut and why, and the actions you would take.\n\n"
        + report_ending(row, offered))


def repair_goal(row, problems, fresh, offered=None):
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"{REPAIR_OPENING}\n\n"
        + (f"{asked(row)}\n\n" if fresh else "")
        + "The report you wrote does not fit its shape. Each problem names the "
        f"field and what it must be:\n\n{listed}\n\n"
        "Write the whole report again with these fixed.\n\n"
        + report_ending(row, offered))


def lead_turn(conn, project, scout_id, seat, argv, worktree, cap, stopping):
    label = outbound(argv_label(argv), known_secrets(project.config()))
    payload = dict(role=SCOUT_LEAD, label=label, route="primary",
                   exit_status=None, timed_out=False)
    started, output = monotonic(), ""
    try:
        try:
            payload["exit_status"], output = stopping.run_capped(
                argv, worktree, cap)
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
    def __init__(self, conn, project, row, seat, worktree, stopping):
        self.conn, self.project, self.row = conn, project, row
        self.seat, self.worktree, self.stopping = seat, worktree, stopping
        self.secrets = known_secrets(project.config())
        self.session = None

    def remember(self, session):
        if session is not None:
            self.session = session
            set_scout_session(self.conn, self.row.id, session)

    def turn(self, argv, cap):
        self.stopping.check()
        return lead_turn(self.conn, self.project, self.row.id, self.seat, argv,
                         self.worktree, cap, self.stopping)

    def outgoing(self, goal, context):
        head, slot, tail = goal.partition(CONTEXT_SLOT)
        return (outbound(head, self.secrets) + (context if slot else "")
                + outbound(tail, self.secrets))

    def start(self, goal, cap, context=""):
        argv = self.seat.turn(self.outgoing(goal, context))
        self.remember(self.seat.session(argv))
        turn = self.turn(argv, cap)
        if self.session is None:
            self.remember(self.seat.reported_session(turn.output))
        return turn

    def resume(self, goal, cap, context=""):
        goal = self.outgoing(goal, context)
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
    if not (tasks and swarm.left() and swarm.ready()):
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


def unoffered(report, offered):
    if offered is not None:
        return report
    return {key: value for key, value in report.items() if key != "prior"}


def checked_report(lead, report, offered=None):
    check = partial(report_problems, kind=lead.row.kind,
                    subject=subject_of(lead.row), offered=offered)
    report = unoffered(report, offered)
    problems = check(report)
    if not problems:
        return report, []
    turn = lead.resume(repair_goal(lead.row, problems, lead.session is None,
                                   offered), REPORT_TURN_S)
    report = fenced_json(turn.answer)
    if not isinstance(report, dict):
        return None, ["report: the repair turn returned no JSON object"]
    report = unoffered(report, offered)
    return report, check(report)


def revised_ticket(conn, row):
    if not offers_revise(subject_of(row)):
        return None
    found = conn.execute(
        "SELECT t.linearIdentifier, t.revision FROM runs r"
        " JOIN tickets t ON t.id = r.ticketId WHERE r.id = ?",
        (row.subject_ref,)).fetchone()
    return None if found is None else {"ticket": found[0],
                                       "revision": found[1]}


def stamped(conn, row, report):
    revises = revised_ticket(conn, row)
    actions = [{**{key: value for key, value in action.items()
                   if key != "revises"},
                **({"revises": revises} if action["kind"] == REVISE else {})}
               for action in report["actions"]]
    return dict(report, actions=actions)


def lead_seat(project, row, config, stopping):
    name = row.lead_route or config.lead
    seat = config.leads.get(name)
    if seat is None:
        return None, f"lead route {name} is down: no [scout.leads.{name}]"
    probe = probe_scout_route(project, f"{SCOUT_LEAD}:{name}", seat,
                              PROBE_TIMEOUT,
                              partial(in_critic_workspace,
                                      run=stopping.run_capped))
    if not probe.ok:
        return None, f"lead route {name} is down: {probe.reason()}"
    return seat, None


def scout_context(conn, project, row):
    pack = context_pack(project, row)
    if pack is None:
        return "", []
    record_scout_event(conn, row.id, "scout_context", pack.summary,
                       level="detail", payload=json.dumps({
                           "subject": f"{row.subject_kind}:{row.subject_ref}",
                           "chars": len(pack.text), "paths": pack.paths}))
    return f"## Context\n\n{pack.text}\n\n", pack.paths


def scout_prior(conn, project, row, pack_paths):
    paths = scout_paths(project.path, row, pack_paths)
    if not paths:
        return "", {}
    priors = prior_reports(conn, project.path, row, paths)
    ids = [prior.scout for prior in priors]
    record_scout_event(
        conn, row.id, "scout_prior", "prior reports: " + (
            f"scouts {', '.join(map(str, ids))}" if ids else "none"),
        level="detail", payload=json.dumps({"scouts": ids, "paths": paths}))
    return (prior_section(project.path, priors, row.base_sha),
            offered_findings(priors))


def run_lead(conn, project, row, worktree, budget_s, stopping):
    """Returns the phase the scout ends in and the reason it ends there."""
    config = scout_config(project)
    limit = row.helper_turns or config.helper_turns
    seat, down = lead_seat(project, row, config, stopping)
    if down is not None:
        return "failed", down
    lead = Lead(conn, project, row, seat, worktree, stopping)
    deadline = monotonic() + budget_s
    context, pack_paths = scout_context(conn, project, row)
    prior, offered = scout_prior(conn, project, row, pack_paths)
    turn = lead.start(plan_goal(row, limit, context, prior), budget_s, context)
    with Swarm(conn, project, row, config, deadline, stopping) as swarm:
        tasks, problem = wave_tasks(conn, swarm, turn, 1, "plan")
        run_waves(conn, lead, swarm, tasks, row.waves or config.waves)
        turn = lead.resume(report_goal(row, swarm, problem,
                                       lead.session is None, context=context,
                                       offered=offered),
                           REPORT_TURN_S, context)
        report = fenced_json(turn.answer)
        if not isinstance(report, dict):
            return "failed", NO_REPORT
        report, problems = checked_report(lead, report, offered)
        if problems:
            return "failed", f"{MALFORMED}: {problems[0]}"
        report = reproduced(row, swarm, resolve_evidence(project.path,
                                                         row.base_sha, report))
        with store.transaction(conn):
            report = stamped(conn, row, report)
            record_scout_report(conn, row.id, 1, dict(report, cut=swarm.cut()))
            record_scout_actions(conn, row.id, 1, report["actions"])
    return "done", "the lead wrote its report"
