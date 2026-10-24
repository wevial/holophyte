import subprocess
import time

from holophyte.agents.probe_log import record_probe
from holophyte.agents.probes import (
    PROBE_GOAL,
    PROBE_TIMEOUT,
    ProbeResult,
    launch_failure_reason,
    probe_diagnostic,
)
from holophyte.agents.review_workspace import critic_workspace
from holophyte.loop.gates import run_capped
from holophyte.redact import safe_print as print
from holophyte.scout.scout_config import scout_config


def scout_routes(config):
    for kind, seats in (("scout-lead", config.leads),
                        ("scout-helper", config.helpers)):
        for name, seat in seats.items():
            yield f"{kind}:{name}", seat


def probe_scout_route(project, name, seat, timeout=None):
    cmd = seat.turn(PROBE_GOAL)
    cap = PROBE_TIMEOUT if timeout is None else timeout
    started = time.time()
    try:
        with critic_workspace(project) as checkout:
            code, out = run_capped(cmd, checkout, cap)
        probe = ProbeResult(cmd, code, out or "", cap, seat=name)
    except subprocess.TimeoutExpired as expired:
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        probe = ProbeResult(cmd, None, partial, cap, seat=name)
    except (OSError, RuntimeError) as failed:
        probe = ProbeResult(cmd, None, "", cap,
                            launch_error=launch_failure_reason(failed),
                            launch_diagnostic=str(failed), seat=name)
    record_probe(project, probe, False, started, time.time())
    return probe


def scout_probe_mode(project):
    passed = True
    for name, seat in scout_routes(scout_config(project)):
        probe = probe_scout_route(project, name, seat)
        print(probe_diagnostic(project, probe))
        passed = passed and probe.ok
    return 0 if passed else 1
