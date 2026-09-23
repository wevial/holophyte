"""Per-dispatch identity and elapsed time, without prompts or arbitrary argv."""
import json
import shlex
import subprocess
from time import monotonic

import store
from holophyte.agent_routes import routes
from holophyte.config import (
    DEFAULT_IMPLEMENTER,
    IMPL_MODEL,
    agent_command,
    review_route,
)
from holophyte.redact import known_secrets, outbound


def turn_label(target, role):
    """First command word plus the first -m/--model value, if configured."""
    active = routes(target).commands.get(role)
    argv = shlex.split(active) if active else agent_command(target, role, "")
    if not active and argv is not None:
        argv = argv[:-1]  # The appended prompt is never label material.
    if argv is None:
        argv = default_argv(target, role)
    return outbound(argv_label(argv), known_secrets(target.config()))


def default_argv(target, role):
    """The route the loop dispatches for a role the config leaves unset."""
    return ([DEFAULT_IMPLEMENTER, "--model", IMPL_MODEL] if role == "implement"
            else ["codex", "--model", review_route(target)[0]])


def argv_label(argv):
    """`argv[0]`, plus the value of its first -m/--model flag."""
    for flag, value in zip(argv[1:], argv[2:]):
        if flag in ("-m", "--model"):
            return f"{argv[0]} {value}"
    return argv[0]


def route_labels(target):
    """The configured label per seat, as `turn_label()` would record it.

    An unset seat gets the default the loop dispatches; an unset writer
    follows the implementer, as `effective_role()` sends it; an unset
    reviewer fallback is None."""
    secrets = known_secrets(target.config())

    def label(role, fallback=False):
        argv = agent_command(target, role, "", fallback=fallback)
        if argv is not None:
            argv = argv[:-1]  # The appended prompt is never label material.
        elif fallback:
            return None
        else:
            argv = default_argv(target, role)
        return outbound(argv_label(argv), secrets)

    implementer = label("implement")
    return {"implementer": implementer,
            "reviewer": label("review"),
            "reviewer_fallback": label("review", fallback=True),
            "adjudicator": label("adjudicate"),
            "writer": (implementer if agent_command(target, "write", "") is None
                       else label("write"))}


def recorded_turn(target, role, routed_role, conn, run_id, launch):
    """Record each attempt, excluding fallback probing and route-switch time."""
    if conn is None or run_id is None:
        return launch()
    payload = dict(role=role, label=turn_label(target, routed_role),
                   route="fallback" if routed_role in routes(target).commands
                   else "primary", exit_status=None, timed_out=False)
    started = monotonic()
    try:
        output = launch()
        payload.update(exit_status=getattr(output, "exit_code", 0),
                       timed_out=getattr(output, "timed_out", False))
        return output
    except subprocess.TimeoutExpired:
        payload["timed_out"] = True
        raise
    except Exception as exc:
        payload["exit_status"] = getattr(exc.__cause__ or exc, "returncode", None)
        raise
    finally:
        payload["seconds"] = monotonic() - started
        store.record_event(conn, run_id, "agent_turn", f"{role} turn ended",
                           level="detail", payload=json.dumps(payload))
