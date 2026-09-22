"""The agent routes: one turn of a role, dispatched and read back as text.

`agent` runs an implementer, reviewer or adjudicator turn -- the default
route for each role, or the `[agents]` command the target configured --
under the process-group cap the gate shares; `agent_route` names what ran, for
the record the round leaves; `publish_review_refs` gives a configured reviewer
the same two `refs/review/*` names the staged default route gets. This is the
factory's only process-spawning surface besides the gate. Nothing here knows
the loop or the board; a run context keeps configured review turns alive.

Third slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import codecs
import contextlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import review_runner
from holophyte import isolation
from holophyte.agent_routes import route_prose, routes, safe_command
from holophyte.agent_turns import recorded_turn
from holophyte.config import (
    AGENT_CONFIG_KEYS,
    DEFAULT_IMPLEMENTER,
    IMPL_EFFORT,
    IMPL_MODEL,
    IMPL_TIMEOUT,
    agent_command,
    budget_scale,
    carry_directories,
    review_profile,
    review_route,
    sweep_config,
)
from holophyte.gates import InfraFailure, run_capped, sh
from holophyte.redact import known_secrets, outbound
from holophyte.redact import safe_print as print

TRANSPORT_SIGNATURES = (
    "ECONNRESET", "ECONNREFUSED", "ENOTFOUND", "ETIMEDOUT", "getaddrinfo",
    "fetch failed", "Could not resolve host", "502 Bad Gateway",
    "503 Service Unavailable", "504 Gateway Timeout", "overloaded_error",
    "network error",
)


def transport_failure(exit_code, output):
    """Identify a failed CLI's transport signature in its output tail."""
    if exit_code is None or exit_code == 0:
        return None
    tail = output[-4000:].casefold()
    return next((sig for sig in TRANSPORT_SIGNATURES
                 if sig.casefold() in tail), None)


class AgentOutput(str):
    """Turn text retaining the dispatched route for outage classification."""

    def __new__(cls, output, command, *, timed_out=False, exit_code=0):
        result = super().__new__(cls, output)
        result.command = command
        result.exit_code = None if timed_out else exit_code
        result.timed_out = timed_out
        return result


class ImplementerOutput(AgentOutput):
    """Output text retaining the implementer CLI's exit status."""

    def __new__(cls, output, exit_code, command=""):
        result = super().__new__(cls, output, command)
        result.exit_code = exit_code
        return result


# The one-line prompt the implementer probe hands a configured route, and the
# word its answer has to contain. Short enough that any harness answering at
# all answers it inside `PROBE_TIMEOUT` seconds; the cap is generous next to a
# healthy CLI's cold start and small next to the implement turn that would
# otherwise be the first evidence of a route that does not answer.
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
    """What one seat probe found: the exact argv it ran, the exit
    code (`None` when the cap ended it or it never started), the output it
    read back, and -- when the command could not be launched at all -- the
    OS's reason, so a missing or non-executable route is a failed probe
    with a name rather than an exception."""

    command: list
    returncode: object
    output: str
    timeout: int
    launch_error: str = None
    seat: str = "implementer"
    expected_commit: str = None

    @property
    def timed_out(self):
        return self.returncode is None and self.launch_error is None

    @property
    def ok(self):
        witness = self.expected_commit or PROBE_WORD
        return self.returncode == 0 and witness in self.output.lower()

    def describe(self):
        """The line(s) the loop prints: the command as a shell would take it,
        then the verdict -- passed, the exit code, or the timeout -- and the
        last lines of what the route said, so a failure is actionable from
        the terminal without re-running it by hand."""
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
        """The last `PROBE_TAIL_LINES` lines the route printed."""
        return self.output.strip().splitlines()[-PROBE_TAIL_LINES:]

    def to_json(self):
        """The result as the daemon's `PUT /config` reply carries it: the
        verdict, the exact argv, the exit code (`null` on the timeout or a
        launch failure, with `timed_out` and `launch_error` saying which),
        the cap and the output's last lines."""
        return {"ok": self.ok, "command": list(self.command),
                "returncode": self.returncode, "timed_out": self.timed_out,
                "launch_error": self.launch_error,
                "timeout": self.timeout, "output": self.tail()}


def probe_diagnostic(target, probe):
    """Safe diagnostic for terminal output and persisted route evidence."""
    safe = replace(probe, command=[safe_command(target, shlex.join(probe.command))])
    return route_prose(target, safe.describe())


def probe_implementer(target, timeout=None):
    """Run the configured `[agents] implementer` once, with `PROBE_GOAL` as
    the goal, and say whether it answered. `None` when the table names no
    implementer or fallback: unconfigured defaults are not probed here.

    Routing is explicit policy, so the exact command a turn would run is the
    one probed: the argv comes from `agent_command()`, the same builder the
    turn uses, with the goal in the same last position. It runs in an empty
    temporary directory -- a harness that reads its cwd finds nothing to act
    on -- under `run_capped`'s process-group cap, so a route that hangs is
    ended with everything it started rather than left behind the loop. A pass
    is exit 0 with `PROBE_WORD` somewhere in the output; anything else, the
    timeout included, is a failure carrying what the route printed. A command
    the OS cannot launch -- an absolute path that does not exist, a file
    without the execute bit -- passes the config check and fails only here,
    so that too is a failed result naming the reason, not an exception: the
    daemon's config write has already landed by the time it probes.
    """
    return probe_seat(target, "implement", timeout=timeout)


def probe_seat(target, role, *, fallback=False, timeout=None):
    """Probe the exact command, in a scratch checkout for review wrappers."""
    goal = REVIEW_PROBE_GOAL if role in ("review", "adjudicate") else PROBE_GOAL
    cmd = agent_command(target, role, goal, fallback=fallback)
    default = cmd is None
    if default:
        if fallback or (agent_command(target, role, "", fallback=True) is None
                        and not (role == "implement" and
                                 isolation.route_for(target).backend == "container")):
            return None
        cmd = ([DEFAULT_IMPLEMENTER, "-p", PROBE_GOAL, "--model", IMPL_MODEL,
                "--effort", IMPL_EFFORT] if role == "implement" else
               ["default-review", review_profile(*review_route(target))])
    cap = PROBE_TIMEOUT if timeout is None else timeout
    sha = None
    with tempfile.TemporaryDirectory(prefix="holophyte-probe-") as scratch:
        try:
            if role in ("review", "adjudicate"):
                sh(["git", "clone", "--shared", "--quiet",
                    str(target.path), scratch])
                sha = sh(["git", "rev-parse", "HEAD"], cwd=scratch).strip()
                publish_review_refs(Path(scratch), sha, sha)
            if default and role != "implement":
                out = review_runner.run_review(
                    repo=Path(scratch), base_sha=sha, candidate_sha=sha,
                    prompt=goal, model=review_route(target)[0],
                    effort=review_route(target)[1],
                    profile=review_profile(*review_route(target)),
                    timeout=cap, verdicts=None, carry=carry_directories(target))
                code = 0
            else:
                if role == "implement":
                    code, out = isolation.launch(
                        replace(isolation.route_for(target), writable=False), scratch,
                        isolation.environment(target), cmd, timeout=cap,
                        runner=run_capped)
                else:
                    code, out = run_capped(cmd, scratch, cap)
        except subprocess.TimeoutExpired as expired:
            partial = expired.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            return ProbeResult(cmd, None, partial, cap, seat=AGENT_CONFIG_KEYS[role],
                               expected_commit=sha)
        except (OSError, RuntimeError, review_runner.ReviewBoundaryError) as failed:
            return ProbeResult(cmd, None, "", cap, launch_error=str(failed),
                               seat=AGENT_CONFIG_KEYS[role], expected_commit=sha)
    return ProbeResult(cmd, code, out or "", cap, seat=AGENT_CONFIG_KEYS[role],
                       expected_commit=sha)


def agent_route(target, role):
    """What ran `role`'s turn, named for the record the round leaves.

    The profile of the container route the config chooses (`codex-sol-medium`
    by default), or the configured command when the target named one. A
    `reviewRounds` row reading `codex-sol-medium` about a round some other
    harness or model ran would be evidence of something that did not happen,
    and the rows are what FINDINGS.md and the fingerprint are built from.
    """
    role = effective_role(target, role)
    command = (routes(target).commands.get(role)
            or (target.config().get("agents") or {}).get(AGENT_CONFIG_KEYS[role])
            or (DEFAULT_IMPLEMENTER if role == "implement" else
                review_profile(*review_route(target))))
    return safe_command(target, command)


def review_refs(run_id):
    """The run's shared-repository names; None supports standalone reviews."""
    prefix = "refs/review" if run_id is None else f"refs/review/{int(run_id)}"
    return f"{prefix}/base", f"{prefix}/candidate"


def cleanup_review_refs(repo, run_id):
    """Remove this run's pair without preventing lease and board close-out."""
    for ref in review_refs(run_id):
        try:
            sh(["git", "update-ref", "-d", ref], cwd=repo)
        except (OSError, RuntimeError) as exc:
            print(f"[holo2] review ref cleanup failed for {ref}: {exc}")


def check_review_refs(repo, run_id, base_sha, candidate_sha):
    """A moved or missing boundary is a factory failure, never a verdict."""
    for ref, sha in zip(review_refs(run_id), (base_sha, candidate_sha)):
        result = subprocess.run(["git", "rev-parse", "--verify", ref],
                                cwd=repo, capture_output=True, text=True)
        if result.returncode or result.stdout.strip() != sha:
            raise InfraFailure(f"review ref changed during turn: {ref}; expected {sha}")


def publish_review_refs(repo, base_sha, candidate_sha, run_id=None):
    """Publish an exact, ancestral commit pair under this run's own names."""
    for sha in (base_sha, candidate_sha):
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
            cwd=repo, capture_output=True, text=True)
        if resolved.returncode or resolved.stdout.strip() != sha:
            raise review_runner.ReviewBoundaryError(
                f"not a full commit SHA in {repo}: {sha}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", base_sha,
                       candidate_sha], cwd=repo).returncode:
        raise review_runner.ReviewBoundaryError(
            f"base {base_sha} is not an ancestor of {candidate_sha}")
    for name, sha in zip(review_refs(run_id), (base_sha, candidate_sha)):
        sh(["git", "update-ref", name, sha], cwd=repo)


def record_session(target, conn, run_id, role, output):
    """Persist host implementer session handles from completed or capped turns."""
    import store
    from holophyte.config import implementer_session

    if role != "implement" or conn is None or run_id is None:
        return
    pattern = implementer_session(target)
    if pattern is None or isolation.route_for(target).backend == "container":
        return
    match = pattern.search(output)
    if match and match.group(1):
        route = "fallback" if role in routes(target).commands else "primary"
        store.record_agent_session(conn, run_id, match.group(1), role, route)


def effective_role(target, role):
    """An absent or startup-refused writer follows the active implementer."""
    if role == "write" and (routes(target).writer_failed
                            or agent_command(target, role, "") is None):
        return "implement"
    return role


def writer_turn(target, goal, cwd, timeout, on_start):
    """Like a configured adjudicator: text-only prompt, host command, scratch.

    The configured wrapper supplies its own read-only sandbox. No review refs
    are needed: the writing prompt already carries the bounded diff.
    """
    goal = outbound(goal, known_secrets(target.config()))
    cmd = agent_command(target, "write", goal)
    cap = min(timeout, 1800) if timeout is not None else 1800
    with review_scratch(cwd) as scratch:
        env = dict(os.environ, HOLOPHYTE_REVIEW_SCRATCH=str(scratch))
        hook = {"on_start": on_start} if on_start is not None else {}
        code, output = run_capped(cmd, cwd, cap, env=env, **hook)
    return AgentOutput(output.strip(), shlex.join(cmd[:-1]), exit_code=code)


def agent(target, role, goal, cwd, *, base_sha=None, candidate_sha=None,
          timeout=None, on_start=None, conn=None, run_id=None, argv=None,
          review_round=None):
    """Account for one role call, including timeout and exceptional returns."""
    from store.working import working

    requested_role = role
    role = effective_role(target, role)
    with working(conn, run_id):
        if role == "write":
            return recorded_turn(target, requested_role, role, conn, run_id,
                                 lambda: writer_turn(
                                     target, goal, cwd, timeout, on_start))
        record_pending_switch(target, role, conn, run_id)
        kwargs = dict(base_sha=base_sha, candidate_sha=candidate_sha,
                      timeout=timeout, on_start=on_start, conn=conn,
                      run_id=run_id, argv=argv, review_round=review_round)

        def launch():
            return recorded_turn(target, requested_role, role, conn, run_id,
                                 lambda: _agent(target, role, goal, cwd, **kwargs))
        output = launch()
        command = getattr(output, "command", agent_route(target, role))
        reason = outage_reason(command, output)
        if (argv is None and reason
                and activate_fallback(target, role, reason, conn, run_id)):
            return launch()
        return output


def _agent(target, role, goal, cwd, *, base_sha=None, candidate_sha=None,
          timeout=None, on_start=None, conn=None, run_id=None, argv=None,
          review_round=None):
    """Run one agent turn for a role. Returns combined output text.

    An `implement` turn runs in a process group of its own under `timeout`
    seconds (`IMPL_TIMEOUT` when the caller names none, and never more --
    both multiplied by the target's `[agents] budget_scale`): a
    `claude -p` that reaches the cap is killed with every subagent and Bash
    child it started, and the turn raises `subprocess.TimeoutExpired` carrying
    what it printed first. Signalling only the CLI left its children
    committing into a worktree the loop had already given up on. `on_start`
    is handed that group's `Popen` as it starts (`GroupKill.arm`), so the
    loop can end the same group from outside the wait when the supervisor
    sweeps the run mid-turn (KO-339). Configured review commands use the
    same group cap and return a failed turn on timeout. Their factory-owned
    scratch worktrees are removed on every exit. They heartbeat when `conn`
    and `run_id` are supplied, at half the target's stale interval.

    `adjudicate` is the terminal pass/fail round. It takes the same
    independent reviewer route as `review` — a fresh dispatch that knows only
    the diff and the ticket. Both roles leave verdict validation to the loop:
    malformed reviews get one reminder, while malformed adjudications are
    recorded and read as FAIL. Staging, execution and parsing remain guarded.

    A configured command replaces the role's route, so an `[agents] reviewer`
    override is also an opt-out of the hardened container the default reviewer
    runs behind. What it does not opt out of is the pair the round is about:
    the exact-SHA requirement is enforced either way, and either way the two
    commits reach the reviewer as `refs/review/RUN/base` and
    `refs/review/RUN/candidate` — the names its prompt uses — the staged checkout
    on the default route, the task worktree on the configured one.
    """
    if role not in AGENT_CONFIG_KEYS:
        raise ValueError(role)
    if role in ("review", "adjudicate") and not (base_sha and candidate_sha):
        raise ValueError(f"{role} requires exact base_sha and candidate_sha")
    goal = outbound(goal, known_secrets(target.config()))
    command = routes(target).commands.get(role)
    cmd = (shlex.split(command) + [goal] if command else
           agent_command(target, role, goal))
    if argv is not None:
        cmd = [outbound(arg, known_secrets(target.config())) for arg in argv] + [goal]
    dispatched_route = shlex.join(cmd[:-1]) if cmd is not None else DEFAULT_IMPLEMENTER
    if cmd is None:
        if role != "implement":
            model, effort = review_route(target)
            try:
                return AgentOutput(review_runner.run_review(
                    repo=Path(cwd),
                    run_id=run_id,
                    base_sha=base_sha,
                    candidate_sha=candidate_sha,
                    prompt=goal,
                    model=model,
                    effort=effort,
                    profile=review_profile(model, effort),
                    timeout=1800,
                    verdicts=None,
                    carry=carry_directories(target),
                ), review_profile(model, effort))
            except review_runner.ReviewBoundaryError as e:
                # The runner could not stage, start or read the reviewer —
                # a missing CLI, an image that will not build, a container
                # that produced no events. The candidate was never judged,
                # so the failure is the factory's, not the ticket's.
                raise InfraFailure(f"reviewer route failed for {role}:"
                                   f" {e}") from e
        cmd = [DEFAULT_IMPLEMENTER, "-p", goal, "--model", IMPL_MODEL,
               "--effort", IMPL_EFFORT]
    elif role != "implement":
        publish_review_refs(Path(cwd), base_sha, candidate_sha, run_id=run_id)
    if role != "implement":
        # runs imports agent_route for round records; defer this import to
        # avoid a cycle at module load time.
        from holophyte.runs import heartbeat_while

        beat_s = sweep_config(target).heartbeat_stale_ms / 2000
        cap = 1800 if timeout is None else min(timeout, 1800)
        try:
            with review_scratch(cwd) as scratch, heartbeat_while(conn, run_id, beat_s):
                env = dict(
                    os.environ, HOLOPHYTE_REVIEW_CANDIDATE=review_refs(run_id)[1],
                    HOLOPHYTE_REVIEW_SCRATCH=str(scratch))
                from holophyte.review_session import prepare_environment, record_session
                route = 'fallback' if role in routes(target).commands else 'primary'
                prepare_environment(target, env, conn, run_id, role, route,
                                    review_round)
                try:
                    return configured_review(cmd, cwd, cap, env, role, dispatched_route)
                finally:
                    record_session(scratch, conn, run_id, role, route, review_round)
        finally:
            check_review_refs(cwd, run_id, base_sha, candidate_sha)
    # `IMPL_TIMEOUT` is the scaled thirty minutes on this target: the
    # caller's `timeout` already carries `[agents] budget_scale`, and the
    # ceiling it is held under stretches with it.
    cap = IMPL_TIMEOUT * budget_scale(target)
    if timeout is not None:
        cap = min(timeout, cap)
    # The hook is passed only when there is one, so a turn without a
    # sweep-time kill runs exactly the call it always did.
    hook = {"on_start": on_start} if on_start is not None else {}
    code, out = isolation.launch(isolation.route_for(target), cwd,
                                 isolation.environment(target), cmd,
                                 timeout=cap, runner=run_capped, target=target, **hook)
    return ImplementerOutput(out.strip(), code, dispatched_route)


@contextlib.contextmanager
def review_scratch(repo):
    """Own the wrapper's scratch space, including git's worktree registrations."""
    env = {key: value for key, value in os.environ.items()
           if key not in {"GIT_DIR", "GIT_COMMON_DIR",
                          "GIT_WORK_TREE", "GIT_INDEX_FILE"}}
    with tempfile.TemporaryDirectory(prefix="holophyte-review-") as scratch:
        try:
            yield Path(scratch)
        finally:
            try:
                for path in review_worktrees(repo, env=env):
                    if path.resolve().is_relative_to(Path(scratch).resolve()):
                        try:
                            sh(["git", "worktree", "remove", "--force", "--force",
                                str(path)], cwd=repo, env=env)
                        except (OSError, RuntimeError) as exc:
                            print(f"[holo2] review worktree cleanup failed: {exc}")
            finally:
                sh(["git", "worktree", "prune"], cwd=repo, env=env)


def review_worktrees(repo, env=None):
    """Read porcelain on Git 2.34 (raw paths) and newer Git (C-quoted paths)."""
    listing = sh(["git", "worktree", "list", "--porcelain"], cwd=repo, env=env)
    # The following HEAD/bare field terminates the path: old Git can emit
    # literal newlines in it. New Git quotes control bytes and uses octal
    # escapes for non-ASCII bytes, which must be decoded before filesystem text.
    for raw in re.findall(r'^worktree (.*?)\n(?:HEAD [0-9a-f]+|bare)(?:\n|$)',
                          listing, re.MULTILINE | re.DOTALL):
        if raw.startswith('"'):
            raw = os.fsdecode(codecs.escape_decode(os.fsencode(raw[1:-1]))[0])
        yield Path(raw)


def configured_review(cmd, cwd, cap, env, role, command):
    """Turn the group cap into a reviewer failure eligible for route fallback."""
    try:
        code, output = run_capped(cmd, cwd, cap, env=env)
    except subprocess.TimeoutExpired:
        message = f"{AGENT_CONFIG_KEYS[role]} timed out after {cap / 60:g} minutes"
        return AgentOutput(message, command, timed_out=True)
    return AgentOutput(output.strip(), command, exit_code=code)


# Exact substrings emitted by the supported routes. Keep causes here so the
# event carries the matching line, rather than a guessed generic failure.
OUTAGE_SIGNATURES = {
    "claude": ("You've hit your limit", "Credit balance is too low"),
    "codex": ("You've hit your usage limit",),
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


def record_pending_switch(target, role, conn, run_id):
    """Startup has no run; attach its switch to the first turn it affects."""
    import store

    state = routes(target)
    if conn is not None and run_id is not None and role in state.pending:
        evidence = state.pending[role]
        store.record_event(conn, run_id, "route_fallback", json.dumps(evidence))
        del state.pending[role]


def activate_fallback(target, role, reason, conn=None, run_id=None, *, probe=None):
    """Probe, record and switch once; a failed fallback never becomes active."""
    from holophyte.runs import open_store
    from store.agent_routes import switched

    state = routes(target)
    command = (target.config().get("agents") or {}).get(
        AGENT_CONFIG_KEYS[role] + "_fallback")
    if not command or role in state.commands:
        return False
    probe = probe or probe_seat(target, role, fallback=True)
    if not probe.ok:
        state.failed = True
        diagnostic = probe_diagnostic(target, probe)
        print(diagnostic)
        raise InfraFailure(diagnostic)
    evidence = {"seat": AGENT_CONFIG_KEYS[role],
                "reason": route_prose(target, reason),
                "command": safe_command(target, command)}
    owned = conn is None
    conn = open_store(target) if owned else conn
    try:
        project = state.project
        if run_id is not None:
            project, = conn.execute("SELECT projectId FROM runs WHERE id=?",
                                    (run_id,)).fetchone()
        if project is None:
            raise InfraFailure("fallback requires a project or run context")
        switched(conn, project, evidence, run_id)
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

def startup_routes(target, provider, implementer_probe=None, *, activate=True):
    """Probe seats; schedulers use activate=False to leave route state alone."""
    import store
    from holophyte.operator import _record_startup_probe
    from holophyte.runs import open_store

    table = target.config().get("agents") or {}
    for role, seat in AGENT_CONFIG_KEYS.items():
        if role != "implement" and seat + "_fallback" not in table:
            continue
        probe = ((implementer_probe or probe_implementer)(target)
                 if role == "implement" else
                 probe_seat(target, role))
        if probe is None:
            continue
        print(probe_diagnostic(target, probe))
        if not probe.ok and seat + "_fallback" in table:
            fallback = probe_seat(target, role, fallback=True)
            if fallback.ok and activate:
                conn = open_store(target)
                try:
                    routes(target).project = store.ensure_project(
                        conn, provider.team, target.path)
                    activate_fallback(target, role, probe_diagnostic(target, probe),
                                      conn, probe=fallback)
                finally:
                    conn.close()
            elif not fallback.ok:
                print(probe_diagnostic(target, fallback))
            probe = fallback
        _record_startup_probe(target, provider, probe)
        if not probe.ok:
            return False
    probe_writer(target, activate=activate)
    return True


def probe_writer(target, *, activate):
    """A prose route failure is reported but never prevents implementation."""
    probe = probe_seat(target, "write")
    if probe is None:
        return
    print(probe_diagnostic(target, probe))
    if not probe.ok:
        print("[holo2] writer route down; using implementer for PR text")
    if activate:
        state = routes(target)
        state.writer_failed = not probe.ok
        state.publish()
