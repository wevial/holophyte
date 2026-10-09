import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import review_runner
from holophyte.agents.agent_routes import route_prose, routes, safe_command
from holophyte.agents.harness import critic_seat
from holophyte.agents.harness import seat as harness_seat
from holophyte.agents.review_workspace import (
    critic_workspace,
    publish_review_refs,
    review_scratch,
    table_review,
)
from holophyte.config.agent_settings import (
    agent_command,
    fallback_entries,
    review_route,
    review_tier,
)
from holophyte.config.reader import (
    ADVERSARY_CLAUDE,
    AGENT_CONFIG_KEYS,
    DEFAULT_IMPLEMENTER,
    IMPL_EFFORT,
    IMPL_MODEL,
    SHA_ROLES,
    adversary_credential,
    review_profile,
)
from holophyte.isolation import launcher
from holophyte.loop.gates import run_capped, sh
from holophyte.redact import safe_print as print

# A prompt any healthy harness answers well inside `PROBE_TIMEOUT` seconds.
PROBE_GOAL = "Reply with the single word: ready"
REVIEW_PROBE_GOAL = (
    "Run the read-only command `git rev-parse HEAD` in the current checkout. "
    "Reply with ready followed by the full commit id from the command output."
)
PROBE_WORD = "ready"
PROBE_TIMEOUT = 90
PROBE_TAIL_LINES = 5


@dataclass(frozen=True)
class ProbeResult:

    command: list
    returncode: object
    output: str
    timeout: int
    launch_error: str = None
    seat: str = "implementer"
    expected_commit: str = None
    entry: int = 0

    @property
    def timed_out(self):
        return self.returncode is None and self.launch_error is None

    @property
    def ok(self):
        witness = self.expected_commit or PROBE_WORD
        return self.returncode == 0 and witness in self.output.lower()

    def describe(self):
        shown = " ".join(shlex.quote(part) for part in self.command)
        if self.ok:
            return f"[holo2] {self.seat} probe passed: {shown}"
        if self.launch_error:
            why = f"could not start: {self.launch_error}"
        elif self.timed_out:
            why = f"no answer within {self.timeout}s"
        elif self.returncode:
            why = f"exit {self.returncode}"
        elif self.expected_commit:
            why = "route answered without reporting the commit; cannot run commands"
        else:
            why = f"exit 0 but no {PROBE_WORD!r} in the output"
        tail = self.tail()
        lines = [f"[holo2] {self.seat} probe failed ({why}): {shown}"]
        lines += [f"[holo2]   | {line}" for line in tail] or [
            "[holo2]   | (no output)"]
        return "\n".join(lines)

    def tail(self):
        return self.output.strip().splitlines()[-PROBE_TAIL_LINES:]

    def to_json(self):
        return {"ok": self.ok, "command": list(self.command),
                "returncode": self.returncode, "timed_out": self.timed_out,
                "launch_error": self.launch_error,
                "timeout": self.timeout, "output": self.tail()}


def probe_diagnostic(project, probe):
    safe = replace(probe, command=[safe_command(project, shlex.join(probe.command))])
    return route_prose(project, safe.describe())


def probe_implementer(project, timeout=None):
    return probe_seat(project, "implement", timeout=timeout)


def probe_seat(project, role, *, fallback=False, timeout=None):
    probe = None
    for entry in range(max(len(fallback_entries(project, role)), 1)
                       if fallback else 1):
        if probe is not None:
            print(probe_diagnostic(project, probe))
        probe = probe_route(project, role, fallback, timeout, entry)
        if probe is None or probe.ok:
            break
    return probe


def probe_route(project, role, fallback, timeout, entry):
    goal = REVIEW_PROBE_GOAL if role in SHA_ROLES else PROBE_GOAL
    cmd = agent_command(project, role, goal, fallback=fallback, entry=entry)
    default = cmd is None
    pair = None
    if default and role == "implement":
        if fallback or (agent_command(project, role, "", fallback=True) is None
                        and launcher.route_for(project).backend != "container"):
            return None
        cmd = [DEFAULT_IMPLEMENTER, "-p", PROBE_GOAL, "--model", IMPL_MODEL,
               "--effort", IMPL_EFFORT]
    elif default:
        pair = (review_route(project, fallback=fallback)
                if role in SHA_ROLES else None)
        if pair is None or not (fallback or container_fallback_profile(project, role)
                                or agent_command(project, role, "", fallback=True)):
            return None
        cmd = ["default-review", review_profile(*pair)]
    cap = PROBE_TIMEOUT if timeout is None else timeout
    sha = None
    with tempfile.TemporaryDirectory(prefix="holophyte-probe-") as scratch:
        try:
            if role in SHA_ROLES:
                sh(["git", "clone", "--shared", "--quiet",
                    str(project.path), scratch])
                sha = sh(["git", "rev-parse", "HEAD"], cwd=scratch).strip()
                publish_review_refs(Path(scratch), sha, sha)
            if default and role != "implement":
                out = review_runner.run_review(
                    repo=Path(scratch), base_sha=sha, candidate_sha=sha,
                    prompt=goal, model=pair[0], effort=pair[1],
                    profile=review_profile(*pair),
                    timeout=cap, verdicts=None,
                    service_tier=review_tier(project, fallback=fallback))
                code = 0
            elif role in ("implement", "trim"):
                code, out = launcher.launch(
                    replace(launcher.turn_route(project, cmd), writable=False), scratch,
                    launcher.environment(project), cmd, timeout=cap,
                    runner=run_capped)
            else:
                code, out = probe_configured_review(project, role, fallback, goal,
                                                    cmd, scratch, cap)
        except subprocess.TimeoutExpired as expired:
            partial = expired.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            return ProbeResult(cmd, None, partial, cap, seat=AGENT_CONFIG_KEYS[role],
                               expected_commit=sha, entry=entry)
        # The config write has already landed: a route that cannot start fails.
        except (OSError, RuntimeError, review_runner.ReviewBoundaryError) as failed:
            return ProbeResult(cmd, None, "", cap, launch_error=str(failed),
                               seat=AGENT_CONFIG_KEYS[role], expected_commit=sha,
                               entry=entry)
    return ProbeResult(cmd, code, out or "", cap, seat=AGENT_CONFIG_KEYS[role],
                       expected_commit=sha, entry=entry)


def probe_adversary_claude(project, timeout=None):
    credential = adversary_credential(project)
    if credential is None or agent_command(project, "adversary", "") is not None:
        return None
    model, effort = ADVERSARY_CLAUDE
    cmd, cap = ["claude", "--model", model], timeout or PROBE_TIMEOUT
    with tempfile.TemporaryDirectory(prefix="holophyte-probe-") as scratch:
        sh(["git", "clone", "--shared", "--quiet", str(project.path), scratch])
        sha = sh(["git", "rev-parse", "HEAD"], cwd=scratch).strip()
        publish_review_refs(Path(scratch), sha, sha)
        try:
            out = review_runner.run_review(
                repo=Path(scratch), base_sha=sha, candidate_sha=sha,
                prompt=REVIEW_PROBE_GOAL, timeout=cap, verdicts=None,
                harness="claude", model=model, effort=effort,
                credential=credential)
        except subprocess.TimeoutExpired:
            return ProbeResult(cmd, None, "", cap, seat="adversary",
                               expected_commit=sha)
        except review_runner.ReviewBoundaryError as failed:
            return ProbeResult(cmd, None, getattr(failed, "output", ""), cap,
                               launch_error=str(failed), seat="adversary",
                               expected_commit=sha)
    return ProbeResult(cmd, 0, out, cap, seat="adversary", expected_commit=sha)


def container_fallback_profile(project, role):
    if (role not in SHA_ROLES
            or AGENT_CONFIG_KEYS[role] in (project.config().get("agents") or {})):
        return None
    pair = review_route(project, fallback=True)
    return review_profile(*pair) if pair else None


def probe_configured_review(project, role, fallback, goal, cmd, clone, cap):
    seat = harness_seat(project, role, fallback=fallback)
    if seat is None:
        return run_capped(cmd, clone, cap)
    env = {key: value for key, value in os.environ.items()
           if key != "HOLOPHYTE_REVIEW_RESUME"}
    with review_scratch(clone) as scratch:
        output = table_review(seat, goal, clone, scratch, cap, env, role)
    if output.timed_out:
        raise subprocess.TimeoutExpired(cmd, cap, output="")
    return output.exit_code, output


def probe_writer(project, *, activate):
    probe = probe_seat(project, "write")
    if probe is None:
        return
    print(probe_diagnostic(project, probe))
    if not probe.ok:
        print("[holo2] writer route down; using implementer for PR text")
    if activate:
        state = routes(project)
        state.writer_failed = not probe.ok
        state.publish()


def probe_critic(project, *, activate, timeout=None):
    seat = critic_seat(project)
    if seat is None:
        return None
    cmd = seat.turn(PROBE_GOAL)
    cap = PROBE_TIMEOUT if timeout is None else timeout
    try:
        with critic_workspace(project) as checkout:
            code, out = run_capped(cmd, checkout, cap)
        probe = ProbeResult(cmd, code, out or "", cap, seat="critic")
    except subprocess.TimeoutExpired as expired:
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        probe = ProbeResult(cmd, None, partial, cap, seat="critic")
    except (OSError, RuntimeError) as failed:
        probe = ProbeResult(cmd, None, "", cap, launch_error=str(failed),
                            seat="critic")
    print(probe_diagnostic(project, probe))
    if not probe.ok:
        print("[holo2] critic route down; claims skip the relevance check")
    if activate:
        routes(project).critic_failed = not probe.ok
    return probe
