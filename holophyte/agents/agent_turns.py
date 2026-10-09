import json
import shlex
import subprocess
from time import monotonic

import store
from holophyte.agents.agent_routes import routes
from holophyte.config.agent_settings import agent_command, review_route
from holophyte.config.reader import DEFAULT_IMPLEMENTER, IMPL_MODEL
from holophyte.redact import known_secrets, outbound


def turn_label(project, role, family_route=None):
    active = routes(project).commands.get(role)
    argv = shlex.split(active) if active else agent_command(project, role, "")
    if not active and argv is not None:
        argv = argv[:-1]  # The appended prompt is never label material.
    if argv is None and family_route is not None:
        return family_label(family_route)
    if argv is None:
        argv = default_argv(project, role)
    return outbound(argv_label(argv), known_secrets(project.config()))


def family_label(family_route):
    return argv_label([family_route["harness"], "--model", family_route["model"]])


def default_argv(project, role):
    return ([DEFAULT_IMPLEMENTER, "--model", IMPL_MODEL] if role == "implement"
            else ["codex", "--model", review_route(project)[0]])


def argv_label(argv):
    for flag, value in zip(argv[1:], argv[2:]):
        if flag in ("-m", "--model"):
            return f"{argv[0]} {value}"
    return argv[0]


def route_labels(project):
    secrets = known_secrets(project.config())

    def label(role, fallback=False):
        argv = agent_command(project, role, "", fallback=fallback)
        if argv is not None:
            argv = argv[:-1]
        elif fallback:
            return None
        else:
            argv = default_argv(project, role)
        return outbound(argv_label(argv), secrets)

    def seat_or_implementer(role):
        return (implementer if agent_command(project, role, "") is None
                else label(role))

    implementer = label("implement")
    return {"implementer": implementer,
            "reviewer": label("review"),
            "reviewer_fallback": label("review", fallback=True),
            "adjudicator": label("adjudicate"),
            "writer": seat_or_implementer("write"),
            "trimmer": seat_or_implementer("trim")}


def recorded_turn(project, role, routed_role, conn, run_id, launch,
                  family_route=None):
    if conn is None or run_id is None:
        return launch()
    payload = dict(role=role, label=turn_label(project, routed_role, family_route),
                   route="fallback" if routed_role in routes(project).commands
                   else "primary", exit_status=None, timed_out=False)
    started = monotonic()
    try:
        output = launch()
        payload.update(exit_status=getattr(output, "exit_code", 0),
                       timed_out=getattr(output, "timed_out", False))
        if hasattr(output, "service_tier"):
            payload["service_tier"] = output.service_tier
        return output
    except Exception as exc:
        cause = exc.__cause__ or exc
        payload["exit_status"] = getattr(cause, "returncode", None)
        payload["timed_out"] = isinstance(cause, subprocess.TimeoutExpired)
        if hasattr(exc, "service_tier"):
            payload["service_tier"] = exc.service_tier
        if payload["timed_out"] and hasattr(exc, "tail"):
            payload["tail"] = exc.tail
        raise
    finally:
        payload["seconds"] = monotonic() - started
        store.record_event(conn, run_id, "agent_turn", f"{role} turn ended",
                           level="detail", payload=json.dumps(payload))
