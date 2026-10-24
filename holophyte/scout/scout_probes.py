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
from holophyte.loop.gates import RunFailure, run_capped
from holophyte.redact import safe_print as print
from holophyte.scout.scout_config import scout_config


def in_critic_workspace(project, cmd, timeout, run=run_capped):
    with critic_workspace(project) as checkout:
        return run(cmd, checkout, timeout)


def probe_scout_route(project, name, seat, timeout=PROBE_TIMEOUT,
                      run=in_critic_workspace):
    cmd = seat.turn(PROBE_GOAL)
    started = time.time()
    try:
        code, out = run(project, cmd, timeout)
        probe = ProbeResult(cmd, code, out or "", timeout, seat=name)
    except subprocess.TimeoutExpired as expired:
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        probe = ProbeResult(cmd, None, partial, timeout, seat=name)
    except (OSError, RuntimeError, RunFailure) as failed:
        probe = ProbeResult(cmd, None, "", timeout,
                            launch_error=launch_failure_reason(failed),
                            launch_diagnostic=str(failed), seat=name)
    record_probe(project, probe, False, started, time.time())
    return probe


def scout_probe_mode(project):
    config = scout_config(project)
    passed = True
    for seats in (config.leads, config.helpers):
        for name, seat in seats.items():
            probe = probe_scout_route(project, f"{seat.role}:{name}", seat)
            print(probe_diagnostic(project, probe))
            passed = passed and probe.ok
    return 0 if passed else 1
