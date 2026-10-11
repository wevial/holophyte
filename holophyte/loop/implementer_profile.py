import dataclasses
import json

import store
import store.read
import ticket_template
from holophyte.agents.agent_routes import ProfileRoutes, routes
from holophyte.agents.agent_turns import turn_label
from holophyte.agents.harness import PROFILES_KEY, implementer_profiles
from holophyte.agents.probes import (
    PROBE_GOAL,
    PROBE_TIMEOUT,
    probe_diagnostic,
    probe_launch,
)
from holophyte.config.config_tables import sweep_config
from holophyte.config.reader import config_table
from holophyte.loop.gates import InfraFailure
from holophyte.loop.runs import heartbeat_while
from holophyte.loop.stop import continuation
from holophyte.redact import safe_print as print

EVENT = "implementer_profile"


def profiled_run(run, task):
    project, conn, run_id = run.project, run.conn, run.run_id
    seats = implementer_profiles(project)
    recorded = carried_profile(run)
    if recorded is not None:
        name = recorded["profile"]
        if name is not None and name not in seats:
            raise InfraFailure(
                f"the run's recorded implementer profile {name!r} is no "
                f"longer configured; restore [agents.{PROFILES_KEY}.{name}] "
                f"to continue it")
        return dataclasses.replace(run, project=scoped(project, name))
    requested = ticket_template.parse(task.get("body") or "").implementer or None
    name, why = None, None
    if requested in seats:
        view = scoped(project, requested)
        beat_s = sweep_config(project).heartbeat_stale_ms / 2000
        with heartbeat_while(conn, run_id, beat_s):
            probe = probe_launch(view, seats[requested].turn(PROBE_GOAL),
                                 PROBE_TIMEOUT, f"{PROFILES_KEY}.{requested}")
        if probe.ok:
            name = requested
        else:
            why = probe_diagnostic(project, probe)
            print(why)
    elif requested is not None:
        why = "unknown profile"
    view = scoped(project, name)
    record(view, conn, run_id, name, requested, why)
    return dataclasses.replace(run, project=view)


def scoped(project, name):
    """The view shares the project's route state but switches its own
    implementer: a default route's fallback does not displace the profile."""
    if name is None:
        return project
    agents = config_table(project, "agents")
    config = dict(project.config(),
                  agents=dict(agents, implementer=agents[PROFILES_KEY][name]))
    view = dataclasses.replace(project, _config=config)
    view._active_agent_routes = ProfileRoutes(routes(project), name)
    return view


def carried_profile(run):
    conn, run_id = run.conn, run.run_id
    row = latest_profile(conn, run_id)
    if row is None or row[0] == run_id:
        return row and row[1]
    resumes = continuation(conn, run_id) is not None or (
        store.read.approved_candidate(
            conn, store.read.run_snapshot(conn, run_id).ticketId, run_id)
        is not None and run.wt.exists())
    return row[1] if resumes else None


def latest_profile(conn, run_id):
    if conn is None or run_id is None:
        return None
    row = conn.execute(
        "SELECT e.runId, e.payload FROM runEvents e JOIN runs r ON r.id = e.runId"
        " JOIN runs current ON current.ticketId = r.ticketId"
        " WHERE current.id = ? AND r.id <= current.id AND e.kind = ?"
        " ORDER BY e.id DESC LIMIT 1", (run_id, EVENT)).fetchone()
    return row and (row[0], json.loads(row[1]))


def run_view(project, conn, run_id):
    row = latest_profile(conn, run_id)
    name = row and row[1]["profile"]
    if name is not None and name not in implementer_profiles(project):
        return None
    return scoped(project, name)


def record(view, conn, run_id, name, requested, why):
    label = turn_label(view, "implement")
    if conn is None or run_id is None:
        return
    summary = (f"implementer profile {name}: {label}" if name is not None
               else f"implementer default: {label}")
    if requested is not None and name is None:
        summary += f" ({requested} requested: {why.splitlines()[0]})"
    store.record_event(conn, run_id, EVENT, summary, level="detail",
                       payload=json.dumps({
                           "profile": name,
                           "source": "default" if name is None else "ticket",
                           "requested": requested, "why": why,
                           "label": label}))


def filing_problems(project, ticket):
    name = ticket.implementer
    if name is None or not ticket_template.IMPLEMENTER_NAME_RE.fullmatch(name):
        return []
    line = f"'Estimate & dependencies' line 'Implementer: {name}'"
    if not project.config_path.exists():
        return [f"{ticket_template.ADVISORY_PREFIX}{line}: profile name not "
                f"checked; the project has no config file"]
    try:
        seats = implementer_profiles(project)
    except SystemExit:
        return [f"{line} checked against [agents], which is malformed"]
    if name in seats:
        return []
    return [f"{line} names no configured profile; configured: "
            f"{', '.join(sorted(seats)) or 'none'}"]
