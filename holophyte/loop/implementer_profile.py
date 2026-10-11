import dataclasses
import json

import store
import ticket_template
from holophyte.agents.agent_routes import routes
from holophyte.agents.agent_turns import turn_label
from holophyte.agents.harness import PROFILES_KEY, implementer_profiles
from holophyte.agents.probes import (
    PROBE_GOAL,
    PROBE_TIMEOUT,
    probe_diagnostic,
    probe_launch,
)
from holophyte.config.reader import config_table
from holophyte.loop.gates import InfraFailure
from holophyte.loop.stop import continuation
from holophyte.redact import safe_print as print

EVENT = "implementer_profile"


def profiled_run(run, task):
    project, conn, run_id = run.project, run.conn, run.run_id
    seats = implementer_profiles(project)
    recorded = recorded_profile(conn, run_id)
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
    """The view shares the project's route state, so an outage switch holds."""
    if name is None:
        return project
    agents = config_table(project, "agents")
    config = dict(project.config(),
                  agents=dict(agents, implementer=agents[PROFILES_KEY][name]))
    view = dataclasses.replace(project, _config=config)
    view._active_agent_routes = routes(project)
    return view


def recorded_profile(conn, run_id):
    if conn is None or run_id is None:
        return None
    row = conn.execute(
        "SELECT e.runId, e.payload FROM runEvents e JOIN runs r ON r.id = e.runId"
        " JOIN runs current ON current.ticketId = r.ticketId"
        " WHERE current.id = ? AND r.id <= current.id AND e.kind = ?"
        " ORDER BY e.id DESC LIMIT 1", (run_id, EVENT)).fetchone()
    if row is None or (row[0] != run_id and continuation(conn, run_id) is None):
        return None
    return json.loads(row[1])


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
