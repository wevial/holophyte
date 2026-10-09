import json
import threading

from holophyte.agents.agent_routes import route_prose, routes, safe_command
from holophyte.agents.probes import (
    container_fallback_profile,
    probe_adversary_claude,
    probe_critic,
    probe_diagnostic,
    probe_implementer,
    probe_seat,
    probe_writer,
)
from holophyte.config.agent_settings import fallback_entries
from holophyte.config.reader import AGENT_CONFIG_KEYS
from holophyte.loop.gates import InfraFailure
from holophyte.redact import safe_print as print

# Exact substrings the supported routes print, so an event quotes the cause.
OUTAGE_SIGNATURES = {
    "claude": ("You've hit your limit", "Credit balance is too low"),
    "codex": ("You've hit your usage limit", "Selected model is at capacity"),
    "devin": ("Quota exhausted", "Usage limit reached",
              "Organization usage limit reached"),
}


def outage_reason(command, output):
    if getattr(output, "timed_out", False):
        return str(output)
    command = command.lower()
    signatures = tuple(sig for route, values in OUTAGE_SIGNATURES.items()
                       if route in command for sig in values)
    return next((line for line in output.splitlines()
                 if any(sig in line for sig in signatures)), None)


def record_pending_switch(project, role, conn, run_id):
    """Startup has no run; its switch goes on the first turn it affects."""
    import store

    state = routes(project)
    if conn is not None and run_id is not None and role in state.pending:
        evidence = state.pending[role]
        store.record_event(conn, run_id, "route_fallback", json.dumps(evidence))
        del state.pending[role]


SWITCHING = threading.RLock()


def activate_fallback(project, role, reason, conn=None, run_id=None, *, probe=None):
    with SWITCHING:
        return _activate(project, role, reason, conn, run_id, probe)


def _activate(project, role, reason, conn, run_id, probe):
    from holophyte.loop.runs import open_store
    from store.agent_routes import switched

    state = routes(project)
    commands = (fallback_entries(project, role)
                or [container_fallback_profile(project, role)])
    if not commands[0] or role in state.commands:
        return False
    probe = probe or probe_seat(project, role, fallback=True)
    if not probe.ok:
        if role == "trim":
            state.trimmer_failed = True
            state.publish()
        else:
            state.failed = True
        diagnostic = probe_diagnostic(project, probe)
        print(diagnostic)
        raise InfraFailure(diagnostic, "infra" if role in ("implement", "trim")
                           else "review_route")
    command = commands[probe.entry]
    evidence = {"seat": AGENT_CONFIG_KEYS[role],
                "reason": route_prose(project, reason),
                "command": safe_command(project, command)}
    owned = conn is None
    conn = open_store(project) if owned else conn
    try:
        project_id = state.project_id
        if run_id is not None:
            project_id, = conn.execute("SELECT projectId FROM runs WHERE id=?",
                                       (run_id,)).fetchone()
        if project_id is None:
            raise InfraFailure("fallback requires a project or run context")
        switched(conn, project_id, evidence, run_id)
    finally:
        if owned:
            conn.close()
    state.commands[role] = command
    if run_id is None:
        state.pending[role] = evidence
    state.publish()
    print(f"[holo2] {evidence['seat']} route down "
          f"({' '.join(evidence['reason'].splitlines())}); "
          f"using fallback: {evidence['command']}")
    return True


def startup_routes(project, provider, implementer_probe=None, *, activate=True,
                   critic=True):
    """A pooled worker passes critic=False and inherits the scheduler's outcome."""
    from holophyte.cli.operator import _record_startup_probe

    table = project.config().get("agents") or {}
    for role, seat in AGENT_CONFIG_KEYS.items():
        has_fallback = seat + "_fallback" in table or (
            role == "review" and container_fallback_profile(project, role))
        if role == "trim" or (role != "implement" and not has_fallback):
            continue
        probe = ((implementer_probe or probe_implementer)(project)
                 if role == "implement" else
                 probe_seat(project, role))
        if probe is None:
            continue
        if role == "adversary" and probe.ok:
            probe = probe_adversary_claude(project) or probe
        print(probe_diagnostic(project, probe))
        if not probe.ok and has_fallback:
            probe = startup_fallback(project, provider, role, probe, activate)
        _record_startup_probe(project, provider, probe)
        if not probe.ok:
            return False
    probe_writer(project, activate=activate)
    probe_trimmer(project, provider, activate=activate)
    if critic:
        # Even a scheduler keeps `critic_failed`: its workers inherit it.
        probe_critic(project, activate=True)
    return True


def startup_fallback(project, provider, role, probe, activate):
    import store
    from holophyte.loop.runs import open_store

    fallback = probe_seat(project, role, fallback=True)
    if fallback.ok and activate:
        conn = open_store(project)
        try:
            routes(project).project_id = store.ensure_project(
                conn, provider.team, project.path)
            activate_fallback(project, role, probe_diagnostic(project, probe),
                              conn, probe=fallback)
        finally:
            conn.close()
    elif not fallback.ok:
        print(probe_diagnostic(project, fallback))
    return fallback


def probe_trimmer(project, provider, *, activate):
    probe = probe_seat(project, "trim")
    if probe is None:
        return
    print(probe_diagnostic(project, probe))
    if not probe.ok and fallback_entries(project, "trim"):
        probe = startup_fallback(project, provider, "trim", probe, activate)
    if not probe.ok:
        print("[holo2] trimmer route down; runs skip trim")
    if activate:
        state = routes(project)
        state.trimmer_failed = not probe.ok
        state.publish()
