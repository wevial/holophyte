"""The implement stage and the timed agent turn every stage runs under."""
import json
import subprocess
from pathlib import Path
from time import monotonic as retry_clock
from time import sleep, time

import store
import store.read
import ticket_template
from holophyte.agents.agent_output import transport_failure
from holophyte.agents.agent_routes import routes
from holophyte.agents.fix_session import resume_argv
from holophyte.agents.harness import ORCHESTRATION_BRIEFS, implementer_orchestrations
from holophyte.agents.roles import agent, record_session
from holophyte.config.agent_settings import budget_scale
from holophyte.config.config_tables import sweep_config, verify_config
from holophyte.config.reader import config_table
from holophyte.environment_git import (
    factory_identity,
    paths,
    stage_work,
    unstage_environment,
)
from holophyte.loop.claim import conflict_brief
from holophyte.loop.gates import GroupKill, InfraFailure, RunFailure, sh
from holophyte.loop.runs import heartbeat_while
from holophyte.loop.stop import boundary
from holophyte.pr.pr_media import implementer_brief as _capture_brief
from holophyte.redact import known_secrets, redact_prose
from holophyte.redact import safe_print as print
from holophyte.review import blast_radius, reproduce
from store.working import agent_work


def _scale_note(project, budget_min):
    scale = budget_scale(project)
    if scale == 1:
        return ""
    return f" ({budget_min * scale:g} min at scale {scale:g})"


def _timed(project, conn, run_id, beat_s, wt, budget_min, goal, *,
           role="implement", argv=None):
    """Return `(output, timed_out)`; a timeout or a sweep kills the turn's group."""
    session_role = role
    kill = GroupKill()
    with heartbeat_while(conn, run_id, beat_s, on_swept=kill):
        try:
            output = agent(project, role, goal, wt,
                           timeout=budget_min * budget_scale(project) * 60,
                           on_start=kill.arm, conn=conn, run_id=run_id,
                           **({"argv": argv} if argv is not None else {}))
            timed_out = False
        except subprocess.TimeoutExpired as expired:
            print(f"[holo2] task exceeded {budget_min} min budget"
                  f"{_scale_note(project, budget_min)}")
            partial = expired.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            partial = partial.strip()
            print(f"[holo2] {'writer' if role == 'write' else 'implementer'}"
                  " output before the budget fired:\n"
                  + (partial[-2000:] or "(no output before the budget fired)"))
            output, timed_out = partial, True
        if not kill.wanted:
            record_session(project, conn, run_id, session_role, output, wt,
                           on_start=kill.arm)
    return output, timed_out


def _open_findings(conn, run_id):
    rounds = store.read.newest_ended_rounds(conn, run_id)
    findings = json.loads(rounds[0].findings) if rounds else []
    items = []
    for finding in findings:
        message = " ".join(str(finding.get("message", "")).split())
        where = str(finding.get("path") or "?")
        if finding.get("line"):
            where += f":{finding['line']}"
        items.append(f"{where}: {message}" if message else where)
    return "; ".join(items) if items else "none on record"


def run_cap_reason(project, conn, run_id, budget_min, sha):
    """Why a turn would take agent work past timeBoxMs × scale × run_cap, or None."""
    if conn is None or run_id is None:
        return None
    run = store.read.run_snapshot(conn, run_id)
    if run is None or not run.timeBoxMs or not budget_min:
        return None
    scale = budget_scale(project)
    cap = sweep_config(project).run_cap
    box_ms = run.timeBoxMs * scale
    spent_ms = agent_work(run, int(time() * 1000))
    if spent_ms is None or spent_ms + budget_min * scale * 60000 <= box_ms * cap:
        return None
    return (f"out of time: {spent_ms / 60000:.1f} min of agent work against a "
            f"{box_ms / 60000:.0f} min box (cap {cap:g}x); candidate "
            f"preserved at {sha[:12]}; open findings: "
            f"{_open_findings(conn, run_id)}")


def _check_run_cap(project, conn, run_id, budget_min, sha):
    reason = run_cap_reason(project, conn, run_id, budget_min, sha)
    if reason is not None:
        store.record_event(conn, run_id, "run_cap", reason)
        raise RunFailure(reason, "budget")


OUTPUT_TAIL = 4000


def _record_implementer_output(conn, run_id, out, secrets=()):
    if conn is None or run_id is None:
        return
    text = redact_prose((out or "").strip(), secrets)
    summary = text.splitlines()[-1] if text else "(implementer printed nothing)"
    store.record_event(conn, run_id, "implementer_output", summary,
                       level="detail", payload=text[-OUTPUT_TAIL:])


def _transport_timed(project, conn, run_id, beat_s, wt, budget_min, goal,
                     argv=None):
    """Retry transport loss once, sharing the original turn's wall-clock cap."""
    scale = budget_scale(project)
    deadline = retry_clock() + budget_min * scale * 60
    remaining = budget_min
    for attempt in range(2):
        out, timed_out = _timed(project, conn, run_id, beat_s, wt,
                                remaining, goal, argv=argv)
        signature = transport_failure(getattr(out, "exit_code", 0), out)
        if timed_out or signature is None or _killed_by_signal(out, timed_out):
            return out, timed_out
        _record_implementer_output(conn, run_id, out,
                                   known_secrets(project.config()))
        reason = f"implementer transport failure ({signature})"
        if attempt:
            raise InfraFailure(f"{reason} after retry; branch preserved")
        note = f"{reason}; retrying once in 30s"
        print(f"[holo2] {note}")
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "transport_retry", note)
        with heartbeat_while(conn, run_id, beat_s):
            sleep(min(30, max(0, deadline - retry_clock())))
        remaining = (deadline - retry_clock()) / (scale * 60)
        if remaining <= 0:
            raise InfraFailure(f"{reason}; retry budget exhausted; branch preserved")


def _commit_wip(project, conn, run_id, wt, branch, task_id, cause):
    # The turn's process group is reaped, so a lock here is the dead
    # turn's; `-uall` lists untracked files rather than their directory.
    lock = Path(wt, sh(["git", "rev-parse", "--git-path", "index.lock"], cwd=wt))
    lock.unlink(missing_ok=True)
    unstage_environment(project, wt)
    dirty = sh(["git", "status", "--porcelain", "-uall", *paths(project)],
               cwd=wt).splitlines()
    if not dirty:
        return None
    stage_work(project, wt)
    sh(["git", *factory_identity(wt), "commit", "-q", "-m",
        f"WIP: implementer {cause} mid-edit ({task_id}); not verified"], cwd=wt)
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    note = (f"{cause} mid-edit; {len(dirty)} changed file(s)"
            f" committed as WIP on {branch} at {head[:12]}")
    print(f"[holo2] {note}")
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "wip_committed", note)
    return head


CRASH_TAIL_LINES = 20


def _killed_by_signal(out, timed_out):
    code = getattr(out, "exit_code", 0)
    return not timed_out and code is not None and (code < 0 or code >= 128)


def _crashed(project, conn, run_id, wt, branch, task_id, out):
    wip = _commit_wip(project, conn, run_id, wt, branch, task_id, "crashed")
    summary = f"implementer killed by a signal (exit {out.exit_code})"
    print(f"[holo2] {summary}")
    if conn is not None and run_id is not None:
        text = redact_prose(out.strip(), known_secrets(project.config()))
        store.record_event(conn, run_id, "crash", summary, level="detail",
                           payload=json.dumps({
                               "exit_status": out.exit_code,
                               "output": "\n".join(
                                   text.splitlines()[-CRASH_TAIL_LINES:])}))
    return wip


def _retry_crashed(project, conn, run_id, beat_s, wt, branch, task_id, goal,
                   out, deadline):
    wip = _crashed(project, conn, run_id, wt, branch, task_id, out)
    note = (f"Your previous turn on this task was killed by a signal (exit"
            f" {out.exit_code}) before it finished."
            + (f" A WIP commit {wip[:12]} on {branch} holds the edits it had"
               " not committed; build on it." if wip else "")
            + " Continue the task and commit your work.")
    remaining = (deadline - retry_clock()) / (budget_scale(project) * 60)
    _check_run_cap(project, conn, run_id, remaining,
                   sh(["git", "rev-parse", "HEAD"], cwd=wt))
    argv, _ = resume_argv(project, conn, run_id)
    out, timed_out = _transport_timed(
        project, conn, run_id, beat_s, wt, remaining,
        note if argv is not None else f"{note}\n\n{goal}", argv=argv)
    if _killed_by_signal(out, timed_out):
        _crashed(project, conn, run_id, wt, branch, task_id, out)
        head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
        raise InfraFailure(f"implementer crashed twice (exit {out.exit_code});"
                           f" work kept on {branch} at {head[:12]}")
    return out, timed_out


def _commands_brief(project, verify_cmd):
    always = "\n".join(verify_config(project).always)
    listed = (f"\n\nThese verify commands must pass before review and again "
              f"before merge:\n\n{verify_cmd}" if verify_cmd else "")
    if always:
        listed += (f"\n\nThe project's baseline checks run after them at "
                   f"every verify gate and must pass too:\n\n{always}")
    return (f"{listed}\n\nThe full unit suite runs as a pull request check; "
            f"do not run it in the worktree. Run only the commands listed "
            f"above." if listed else "")


def _orchestration(project, conn, run_id, ticket):
    requested, source = ticket_template.parse(ticket).orchestration, "ticket"
    if requested not in ticket_template.ORCHESTRATION_MODES:
        table = config_table(project, "agents").get("implementer")
        configured = (table.get("orchestration")
                      if isinstance(table, dict) else None)
        requested, source = ((configured, "project") if configured
                             else ("off", "default"))
    supported = implementer_orchestrations(
        project, routes(project).commands.get("implement")) | {"off"}
    mode = requested
    if mode not in supported:
        mode = "subagents" if "subagents" in supported else "off"
    if conn is not None and run_id is not None:
        summary = f"orchestration: {mode}" + (
            f" ({requested} requested; the route cannot run it)"
            if mode != requested else "")
        store.record_event(conn, run_id, "orchestration", summary,
                           level="detail", payload=json.dumps(
                               {"mode": mode, "requested": requested,
                                "source": source}))
    return ORCHESTRATION_BRIEFS[mode]


def _implement(project, conn, run_id, task_id, task, branch, wt, fresh, beat_s,
               start_sha, ticket, verify_cmd, budget_min, conflicts=(), opening=""):
    commands = _commands_brief(project, verify_cmd)
    _check_run_cap(project, conn, run_id, budget_min, start_sha)
    orchestration = _orchestration(project, conn, run_id, ticket)
    goal = (conflict_brief(branch, conflicts) + opening
            + f"Implement this task in this repo:\n\n{ticket}{commands}\n\n"
            "The ticket above is the contract, acceptance criteria "
            "included; the task is done only when they hold. Commit your "
            "work with a clear message. Stay strictly on-scope; do not "
            "expand the task. Commit messages carry no tool attribution or "
            "co-author lines for an AI." + orchestration
            + _capture_brief(project, ticket, task_id) + blast_radius.BRIEF
            + reproduce.BRIEF)
    deadline = retry_clock() + budget_min * budget_scale(project) * 60
    out, timed_out = _transport_timed(project, conn, run_id, beat_s, wt,
                                      budget_min, goal)
    if _killed_by_signal(out, timed_out):
        out, timed_out = _retry_crashed(project, conn, run_id, beat_s, wt,
                                        branch, task_id, goal, out, deadline)
    blast_radius.record_declared(conn, run_id, out)
    boundary(conn, run_id, "verifying", unreproduced=reproduce.declared(out))
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    # A reused branch already ahead of main is the candidate, even if the
    # turn adds nothing; it still owes verify and review.
    carried = not fresh and bool(
        subprocess.run(["git", "diff", "--quiet", "main", "HEAD"],
                       cwd=wt, capture_output=True).returncode)
    if head == start_sha and not carried and timed_out:
        head = _commit_wip(project, conn, run_id, wt, branch, task_id,
                           "budget fired") or head
    if head == start_sha and not carried:
        print(f"[holo2] implementer made no commits for: {task}")
        _record_implementer_output(conn, run_id, out,
                                   known_secrets(project.config()))
        if fresh:
            sh(["git", "worktree", "remove", "--force", str(wt)], project.path)
            sh(["git", "branch", "-D", branch], project.path)
            raise RunFailure("implementer made no commits; the empty branch"
                             " and worktree were discarded",
                             "budget" if timed_out else "no_commits")
        raise RunFailure(f"implementer made no new commits; preserved work"
                         f" kept on {branch} at {start_sha[:12]}",
                         "budget" if timed_out else "no_commits")
    if head == start_sha:
        note = (f"candidate carried from a prior run; implementer added"
                f" nothing to {branch} at {start_sha[:12]}")
        print(f"[holo2] {note}")
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "carried_candidate", note)
    if timed_out:
        raise RunFailure(f"implementer exceeded the {budget_min} min budget"
                         f"{_scale_note(project, budget_min)}; work kept on "
                         f"{branch} at {head[:12]}", "budget")
    return sh(["git", "rev-parse", "HEAD"], cwd=wt), reproduce.declared(out)
