import json
import os
import shlex
import subprocess
from pathlib import Path

import review_runner
from holophyte.agents.agent_output import AgentOutput, ImplementerOutput
from holophyte.agents.agent_routes import routes, safe_command
from holophyte.agents.agent_turns import recorded_turn
from holophyte.agents.fallback import (
    activate_fallback,
    outage_reason,
    record_pending_switch,
)
from holophyte.agents.harness import agent_session, critic_seat, route_text
from holophyte.agents.harness import seat as harness_seat
from holophyte.agents.probes import PROBE_TIMEOUT, container_fallback_profile
from holophyte.agents.review_workspace import (
    check_review_refs,
    configured_review,
    publish_review_refs,
    review_refs,
    review_scratch,
    table_review,
)
from holophyte.config.agent_settings import (
    agent_command,
    budget_scale,
    review_route,
    review_tier,
)
from holophyte.config.config_tables import sweep_config
from holophyte.config.reader import (
    AGENT_CONFIG_KEYS,
    DEFAULT_IMPLEMENTER,
    IMPL_EFFORT,
    IMPL_MODEL,
    IMPL_TIMEOUT,
    review_profile,
)
from holophyte.config.worktree_settings import carry_directories
from holophyte.isolation import launcher
from holophyte.loop.gates import GroupKill, InfraFailure, run_capped
from holophyte.redact import known_secrets, outbound

REVIEW_TIMEOUT = 1800


def agent_route(project, role):
    role = effective_role(project, role)
    command = (routes(project).commands.get(role)
            or route_text((project.config().get("agents") or {}).get(
                AGENT_CONFIG_KEYS[role]))
            or (DEFAULT_IMPLEMENTER if role == "implement" else
                review_profile(*review_route(project))))
    return safe_command(project, command)


def effective_role(project, role):
    if role == "trim":
        return "implement"
    if role == "write" and (routes(project).writer_failed
                            or agent_command(project, role, "") is None):
        return "implement"
    return role


def record_session(project, conn, run_id, role, output, cwd=None,
                   on_start=None):
    import store
    from holophyte.config.agent_settings import implementer_session

    if role != "implement" or conn is None or run_id is None:
        return
    if launcher.route_for(project).backend == "container":
        return
    fallback = role in routes(project).commands
    seat = None if fallback else harness_seat(project, role)
    if seat is not None:
        session = seat.reported_session(output, None if cwd is None else (
            lambda argv, timeout: run_capped(argv, cwd, timeout,
                                             on_start=on_start,
                                             stderr=subprocess.DEVNULL)))
    else:
        pattern = implementer_session(project)
        match = pattern.search(output) if pattern is not None else None
        session = match.group(1) if match else None
    if session:
        store.record_agent_session(conn, run_id, session, role,
                                   "fallback" if fallback else "primary")


def writer_turn(project, goal, cwd, timeout, on_start):
    """The configured wrapper supplies its own read-only sandbox."""
    goal = outbound(goal, known_secrets(project.config()))
    cmd = agent_command(project, "write", goal)
    cap = min(timeout, 1800) if timeout is not None else 1800
    with review_scratch(cwd) as scratch:
        env = dict(os.environ, HOLOPHYTE_REVIEW_SCRATCH=str(scratch))
        hook = {"on_start": on_start} if on_start is not None else {}
        code, output = run_capped(cmd, cwd, cap, env=env, **hook)
    return AgentOutput(output.strip(), shlex.join(cmd[:-1]), exit_code=code)


def critic_turn(project, goal, cwd, timeout):
    """No fallback: the critic is advice, and a claim goes ahead when it fails."""
    seat = critic_seat(project)
    argv = seat.turn(outbound(goal, known_secrets(project.config())))
    code, output = run_capped(argv, cwd, PROBE_TIMEOUT if timeout is None
                              else timeout)
    return AgentOutput(output.strip(), seat.named(argv), exit_code=code)


def agent(project, role, goal, cwd, *, base_sha=None, candidate_sha=None,
          timeout=None, on_start=None, conn=None, run_id=None, argv=None,
          review_round=None):
    from store.working import working

    requested_role = role
    role = effective_role(project, role)
    with working(conn, run_id):
        if role == "write":
            return recorded_turn(project, requested_role, role, conn, run_id,
                                 lambda: writer_turn(
                                     project, goal, cwd, timeout, on_start))
        if role == "critic":
            return recorded_turn(project, requested_role, role, conn, run_id,
                                 lambda: critic_turn(project, goal, cwd, timeout))
        record_pending_switch(project, role, conn, run_id)
        kwargs = dict(base_sha=base_sha, candidate_sha=candidate_sha,
                      timeout=timeout, on_start=on_start, conn=conn,
                      run_id=run_id, argv=argv, review_round=review_round,
                      session=requested_role == "implement")

        def launch():
            try:
                return recorded_turn(
                    project, requested_role, role, conn, run_id,
                    lambda: _agent(project, role, goal, cwd, **kwargs))
            except InfraFailure as failure:
                record_review_boundary(conn, run_id, role, failure)
                raise
        try:
            output = launch()
        except InfraFailure as failure:
            reason = argv is None and route_down(project, role, failure)
            if reason and activate_fallback(project, role, reason, conn, run_id):
                return second_attempt(launch, failure)
            if timed_out(failure):
                output = second_attempt(launch, failure)
            elif reason or unreadable_output(role, failure) is None:
                raise
            else:
                output = launch()
        command = getattr(output, "command", agent_route(project, role))
        reason = outage_reason(command, output)
        if (argv is None and reason
                and activate_fallback(project, role, reason, conn, run_id)):
            return launch()
        return output


def route_down(project, role, failure):
    reason = outage_reason(agent_route(project, role),
                           getattr(failure, "output", ""))
    if reason:
        return reason
    if (isinstance(failure.__cause__, review_runner.ReviewBoundaryError)
            and container_fallback_profile(project, role)):
        return str(failure)
    return None


def timed_out(failure):
    return isinstance(failure.__cause__, subprocess.TimeoutExpired)


def second_attempt(launch, failure):
    try:
        output = launch()
    except InfraFailure as again:
        if not (timed_out(failure) and timed_out(again)):
            raise
        raise InfraFailure(f"{failure} (twice)", "review_route") from again
    if timed_out(failure) and getattr(output, "timed_out", False):
        raise InfraFailure(f"{failure} (twice)", "review_route")
    return output


def unreadable_output(role, failure):
    cause = failure.__cause__
    if (role == "review" and isinstance(cause, review_runner.ReviewBoundaryError)
            and cause.line is not None):
        return cause
    return None


def record_review_boundary(conn, run_id, role, failure):
    error = unreadable_output(role, failure)
    if error is None or conn is None or run_id is None:
        return
    import store
    store.record_event(conn, run_id, "review_boundary",
                       f"{role} turn output unreadable: {error}", level="detail",
                       payload=json.dumps({"role": role, "line": error.line,
                                           "tail": error.tail,
                                           "exit_status": error.exit_status}))


def kept_session(project, role, conn, run_id, review_round, sessions):
    if role != "review" or None in (conn, run_id, review_round):
        return {}
    return {"transcripts": project.holo_dir / "transcripts",
            "on_session": sessions.append}


def container_review(project, role, goal, cwd, base_sha, candidate_sha, conn,
                     run_id, switched, review_round=None):
    from holophyte.loop.runs import heartbeat_while
    from holophyte.review.review_session import record_review_session
    model, effort = review_route(project, fallback=switched)
    tier = review_tier(project, fallback=switched)
    profile = review_profile(model, effort)
    tiered = {} if tier is None else {"service_tier": tier}
    sessions = []
    kept = kept_session(project, role, conn, run_id, review_round, sessions)
    # A kill ends the container's client; the runner removes the container.
    kill = GroupKill()
    beat_s = sweep_config(project).heartbeat_stale_ms / 2000
    try:
        with heartbeat_while(conn, run_id, beat_s, on_swept=kill):
            output = AgentOutput(review_runner.run_review(
                repo=Path(cwd),
                run_id=run_id,
                base_sha=base_sha,
                candidate_sha=candidate_sha,
                prompt=goal,
                model=model,
                effort=effort,
                profile=profile,
                timeout=REVIEW_TIMEOUT,
                verdicts=None,
                carry=carry_directories(project),
                on_start=kill.arm,
                **tiered,
                **kept,
            ), profile)
        output.service_tier = tier
        for session in sessions:
            record_review_session(conn, run_id, session, role,
                                  "fallback" if switched else "primary",
                                  review_round)
        return output
    except review_runner.ReviewBoundaryError as e:
        # The candidate was never judged: the failure is the factory's.
        failure = InfraFailure(f"reviewer route failed for {role}:"
                               f" {e}", "review_route")
        failure.output = getattr(e, "output", "")
        failure.service_tier = tier
        raise failure from e
    except subprocess.TimeoutExpired as e:
        e.service_tier = tier
        if role != "review":
            raise
        failure = InfraFailure(
            f"review timed out after {REVIEW_TIMEOUT}s", "review_route")
        failure.output = AgentOutput(str(failure), profile, timed_out=True)
        failure.service_tier = tier
        failure.tail = f"{e.output or ''}{e.stderr or ''}"[
            -review_runner.EVIDENCE_TAIL:]
        raise failure from e


def _agent(project, role, goal, cwd, *, base_sha=None, candidate_sha=None,
          timeout=None, on_start=None, conn=None, run_id=None, argv=None,
          review_round=None, session=False):
    if role not in AGENT_CONFIG_KEYS:
        raise ValueError(role)
    if role in ("review", "adjudicate") and not (base_sha and candidate_sha):
        raise ValueError(f"{role} requires exact base_sha and candidate_sha")
    goal = outbound(goal, known_secrets(project.config()))
    switched = (role in routes(project).commands
                and container_fallback_profile(project, role) is not None)
    command = None if switched else routes(project).commands.get(role)
    cmd = (shlex.split(command) + [goal] if command else
           agent_command(project, role, goal))
    session_id = (agent_session(project, role, cmd)
                  if session and command is None and argv is None else None)
    if argv is not None:
        cmd = [outbound(arg, known_secrets(project.config())) for arg in argv] + [goal]
    seat = harness_seat(project, role) if command is None else None
    dispatched_route = (seat.named(cmd) if seat is not None else
                        shlex.join(cmd[:-1]) if cmd is not None else
                        DEFAULT_IMPLEMENTER)
    if cmd is None:
        if role != "implement":
            return container_review(project, role, goal, cwd, base_sha,
                                    candidate_sha, conn, run_id, switched,
                                    review_round)
        cmd = [DEFAULT_IMPLEMENTER, "-p", goal, "--model", IMPL_MODEL,
               "--effort", IMPL_EFFORT]
    elif role != "implement":
        publish_review_refs(Path(cwd), base_sha, candidate_sha, run_id=run_id)
    if role != "implement":
        # runs imports agent_route: importing it at load would be a cycle.
        from holophyte.loop.runs import heartbeat_while
        from holophyte.review.review_session import prepare_environment, record_session

        beat_s = sweep_config(project).heartbeat_stale_ms / 2000
        cap = REVIEW_TIMEOUT if timeout is None else min(timeout, REVIEW_TIMEOUT)
        kill = GroupKill()
        try:
            with review_scratch(cwd) as scratch:
                with heartbeat_while(conn, run_id, beat_s, on_swept=kill):
                    env = dict(
                        os.environ,
                        HOLOPHYTE_REVIEW_CANDIDATE=review_refs(run_id)[1],
                        HOLOPHYTE_REVIEW_SCRATCH=str(scratch))
                    route = ('fallback' if role in routes(project).commands
                             else 'primary')
                    prepare_environment(project, env, conn, run_id, role, route,
                                        review_round)
                    if seat is not None:
                        output = table_review(seat, goal, cwd, scratch, cap, env,
                                              role, run_id=run_id, conn=conn,
                                              kill=kill)
                    else:
                        output = configured_review(cmd, cwd, cap, env, role,
                                                   dispatched_route,
                                                   on_start=kill.arm)
                # Past the heartbeat, which raises for a swept run: no session.
                record_session(scratch, conn, run_id, role, route, review_round)
                return output
        finally:
            check_review_refs(cwd, run_id, base_sha, candidate_sha)
    # A caller's timeout already carries budget_scale; the ceiling stretches too.
    cap = IMPL_TIMEOUT * budget_scale(project)
    if timeout is not None:
        cap = min(timeout, cap)
    hook = {"on_start": on_start} if on_start is not None else {}
    if session_id is not None and conn is not None and run_id is not None:
        # Recorded before launch, so a turn the budget kills still leaves it.
        import store
        store.record_agent_session(conn, run_id, session_id, role, "primary")
    code, out = launcher.launch(launcher.route_for(project), cwd,
                                 launcher.environment(project), cmd,
                                 timeout=cap, runner=run_capped, project=project,
                                 keep_session=True, **hook)
    return ImplementerOutput(out.strip(), code, dispatched_route)
