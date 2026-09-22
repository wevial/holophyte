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
        argv = ([DEFAULT_IMPLEMENTER, "--model", IMPL_MODEL] if role == "implement"
                else ["codex", "--model", review_route(target)[0]])
    label = argv[0]
    for flag, value in zip(argv[1:], argv[2:]):
        if flag in ("-m", "--model"):
            label += " " + value
            break
    return outbound(label, known_secrets(target.config()))


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
