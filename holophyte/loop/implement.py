"""The implement stage and the timed agent turn every stage runs under."""
import json
import os
import re
import sqlite3
import subprocess
from contextlib import suppress
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
from holophyte.config.agent_settings import budget_scale, turn_cap, turn_cap_min
from holophyte.config.config_tables import sweep_config, verify_config
from holophyte.config.reader import config_table
from holophyte.environment_git import (
    factory_identity,
    paths,
    protected,
    stage_work,
    unstage_environment,
)
from holophyte.leak_guard import register_matches
from holophyte.loop.claim import conflict_brief, mid_merge, unmerged_paths
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


def _armed(project, budget_min, seconds=None, limit=None):
    cap = turn_cap(project)
    requested = round(budget_min * budget_scale(project) * 60
                      if seconds is None else seconds)
    return min(cap, requested), limit or (
        "turn_cap" if cap < requested else "time_box")


def implement_arming(project, conn, run_id, budget_min):
    scale = budget_scale(project)
    run = (store.read.run_snapshot(conn, run_id)
           if conn is not None and run_id is not None else None)
    spent_ms = agent_work(run, int(time() * 1000)) if run is not None else None
    floor = min(10, budget_min) * 60 * scale
    left = budget_min * 60 * scale - (spent_ms or 0) // 1000
    return _armed(project, budget_min, max(floor, left),
                  "time_box" if floor > left else None)


def _limit_text(project, limit, budget_min):
    if limit == "turn_cap":
        return (f"{turn_cap(project)} s turn cap ([agents] turn_cap_min"
                f" = {turn_cap_min(project)}) under a {budget_min} min box"
                f"{_scale_note(project, budget_min)}")
    return f"{budget_min} min budget{_scale_note(project, budget_min)}"


WIP_PREFIX = "WIP: implementer "


def _timed(project, conn, run_id, beat_s, wt, budget_min, goal, *,
           role="implement", argv=None, seconds=None, limit=None, sweep=True):
    """Return `(output, timed_out)`; a timeout or a sweep kills the turn's group."""
    swept = sweep and role in ("implement", "trim")
    try:
        output, timed_out = _run_turn(project, conn, run_id, beat_s, wt,
                                      budget_min, goal, role, argv, seconds,
                                      limit)
    except Exception:
        if swept:
            _sweep_quietly(project, conn, run_id, wt, "ended")
        raise
    if swept:
        _sweep_quietly(project, conn, run_id, wt, _turn_end(output, timed_out))
    return output, timed_out


def _run_turn(project, conn, run_id, beat_s, wt, budget_min, goal, role, argv,
              seconds, limit):
    session_role = role
    armed, limit = _armed(project, budget_min, seconds, limit)
    kill = GroupKill()
    with heartbeat_while(conn, run_id, beat_s, on_swept=kill):
        try:
            output = agent(project, role, goal, wt, timeout=armed,
                           on_start=kill.arm, conn=conn, run_id=run_id,
                           **({"argv": argv} if argv is not None else {}))
            timed_out = False
        except subprocess.TimeoutExpired as expired:
            text = _limit_text(project, limit, budget_min)
            print(f"[holo2] task exceeded {text}")
            if conn is not None and run_id is not None:
                store.record_event(
                    conn, run_id, "turn_timeout", f"{role} turn exceeded {text}",
                    level="detail", payload=json.dumps(
                        {"role": role, "limit": limit, "seconds": armed}))
            partial = expired.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            partial = partial.strip()
            register_matches(project, partial)
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
                     argv=None, *, seconds=None, limit=None):
    """Retry transport loss once, sharing the original turn's wall-clock cap."""
    remaining, limit = _armed(project, budget_min, seconds, limit)
    deadline = retry_clock() + remaining
    for attempt in range(2):
        out, timed_out = _timed(project, conn, run_id, beat_s, wt, budget_min,
                                goal, argv=argv, seconds=remaining, limit=limit)
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
        remaining = deadline - retry_clock()
        if remaining <= 0:
            raise InfraFailure(f"{reason}; retry budget exhausted; branch preserved")


def _announce(conn, run_id, kind, note):
    print(f"[holo2] {note}")
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, kind, note)


def _turn_end(out, timed_out):
    if timed_out:
        return "budget fired"
    if _killed_by_signal(out, timed_out):
        return "crashed"
    return "failed" if getattr(out, "exit_code", 0) else "stopped"


def _wip_subject(cause, task_id):
    return f"{WIP_PREFIX}{cause} mid-edit ({task_id}); not verified"


def _task_key(conn, run_id, branch):
    run = (store.read.run_snapshot(conn, run_id)
           if conn is not None and run_id is not None else None)
    ticket = run and store.read.ticket_by_id(conn, run.ticketId)
    return ticket.linearIdentifier if ticket else branch


def _sweep_quietly(project, conn, run_id, wt, cause):
    try:
        _sweep_tree(project, conn, run_id, wt, cause)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError,
            sqlite3.Error) as failed:
        print(f"[holo2] the turn-end sweep of {wt} failed: {failed}")


def _sweep_tree(project, conn, run_id, wt, cause):
    if not Path(wt).is_dir() or subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=wt,
            capture_output=True).returncode:
        return
    # The turn's process group is reaped, so a lock here is the dead turn's.
    lock = Path(wt, sh(["git", "rev-parse", "--git-path", "index.lock"], cwd=wt))
    lock.unlink(missing_ok=True)
    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    task_id = _task_key(conn, run_id, branch)
    if mid_merge(wt):
        _sweep_merge(project, conn, run_id, wt, branch, task_id)
    _commit_wip(project, conn, run_id, wt, branch, task_id, cause)


def _commit_wip(project, conn, run_id, wt, branch, task_id, cause):
    unstage_environment(project, wt)
    # `-uall` lists untracked files rather than their directory.
    dirty = sh(["git", "status", "--porcelain", "-uall", *paths(project)],
               cwd=wt).splitlines()
    if not dirty:
        return
    stage_work(project, wt)
    sh(["git", *factory_identity(wt), "commit", "-q", "-m",
        _wip_subject(cause, task_id)], cwd=wt)
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    _announce(conn, run_id, "wip_committed",
              f"{cause} mid-edit; {len(dirty)} changed file(s)"
              f" committed as WIP on {branch} at {head[:12]}")


def _sweep_merge(project, conn, run_id, wt, branch, task_id):
    tree, conflicted = _merge_tree(wt)
    unresolved = unmerged_paths(wt) or _still_marked(wt, conflicted)
    if not unresolved:
        stage_work(project, wt)
        sh(["git", *factory_identity(wt), "commit", "-q", "--no-edit"], cwd=wt)
        _drop_stash_clashes(wt)
        head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
        _announce(conn, run_id, "merge_completed",
                  f"the turn left a resolved merge uncommitted; committed it"
                  f" on {branch} at {head[:12]}")
        return
    merged = (_listed(wt, "diff", "--name-only", "--no-renames", "-z", "HEAD",
                      tree) | conflicted | set(unresolved))
    backup = _backup_resolution(project, wt, task_id)
    _announce(conn, run_id, "merge_aborted",
              f"the turn left the merge on {branch} unresolved in"
              f" {', '.join(unresolved)}; aborted it, its attempted resolution"
              f" backed up at {backup}")
    _unwind_merge(wt, merged)


def _listed(wt, *args):
    return set(filter(None, subprocess.run(
        ["git", "--literal-pathspecs", *args], cwd=wt, check=True,
        capture_output=True, text=True).stdout.split("\0")))


def _merge_tree(wt):
    merge = subprocess.run(["git", "merge-tree", "--write-tree", "--no-messages",
                            "--name-only", "-z", "HEAD", "MERGE_HEAD"], cwd=wt,
                           capture_output=True, text=True)
    if merge.returncode not in (0, 1):
        raise RuntimeError(f"git merge-tree failed:\n{merge.stderr}")
    tree, *conflicted = merge.stdout.split("\0")
    return tree, set(filter(None, conflicted))


CONFLICT_MARKER = re.compile(rb"^(<{7}|>{7})( |$)", re.MULTILINE)


def _still_marked(wt, conflicted):
    return sorted(path for path in conflicted
                  if not Path(wt, path).is_symlink() and Path(wt, path).is_file()
                  and CONFLICT_MARKER.search(Path(wt, path).read_bytes()))


def _remove_file(wt, path):
    leftover = Path(wt, path)
    if leftover.is_symlink() or leftover.is_file():
        leftover.unlink()
        with suppress(OSError):
            os.removedirs(leftover.parent)


def _take_autostash(wt):
    ref = Path(wt, sh(["git", "rev-parse", "--git-path", "MERGE_AUTOSTASH"],
                      cwd=wt))
    if not ref.is_file():
        return None
    stash = ref.read_text().strip()
    ref.unlink()
    return stash


def _restore_to_head(wt, chosen):
    kept = chosen & _listed(wt, "ls-tree", "-r", "--name-only", "-z", "HEAD",
                            "--", *chosen)
    for path in sorted(chosen - kept):
        _remove_file(wt, path)
    if kept:
        sh(["git", "--literal-pathspecs", "checkout", "-q", "HEAD", "--",
            *sorted(kept)], cwd=wt)


def _drop_stash_clashes(wt):
    clashed = set(unmerged_paths(wt))
    if clashed:
        sh(["git", "reset", "-q"], cwd=wt)
        _restore_to_head(wt, clashed)


def _unwind_merge(wt, merged):
    autostash = _take_autostash(wt)
    sh(["git", "reset", "-q"], cwd=wt)
    _restore_to_head(wt, merged)
    if autostash and subprocess.run(["git", "stash", "apply", "-q", autostash],
                                    cwd=wt, capture_output=True).returncode:
        _drop_stash_clashes(wt)
        sh(["git", "stash", "store", "-m", "autostash", autostash], cwd=wt)


def _staged_entries(project, wt):
    listed = subprocess.run(["git", "ls-files", "-s", "-z"], cwd=wt, check=True,
                            capture_output=True, text=True).stdout
    kept = []
    for entry in filter(None, listed.split("\0")):
        info, _, path = entry.partition("\t")
        mode, sha, stage = info.split()
        if stage in ("0", "2") and not (protected(project) and path == ".env"):
            kept.append(f"{mode} {sha} 0\t{path}\0")
    return "".join(kept)


def _backup_resolution(project, wt, task_id):
    index = Path(wt, sh(["git", "rev-parse", "--git-path",
                         "holophyte-backup.index"], cwd=wt))
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    staged = _staged_entries(project, wt)
    index.unlink(missing_ok=True)
    try:
        subprocess.run(["git", "update-index", "-z", "--index-info"], cwd=wt,
                       env=env, input=staged, text=True, check=True,
                       capture_output=True)
        staged_tree = sh(["git", "write-tree"], wt, env)
        sh(["git", "read-tree", "HEAD"], wt, env)
        sh(["git", "add", "-A", *paths(project)], wt, env)
        tree = sh(["git", "write-tree"], wt, env)
    finally:
        index.unlink(missing_ok=True)
    identity = factory_identity(wt)
    staged_commit = sh(["git", *identity, "commit-tree", staged_tree, "-p",
                        "HEAD", "-m", f"backup: staged merge resolution"
                        f" ({task_id})"], cwd=wt)
    return sh(["git", *identity, "commit-tree", tree, "-p", "HEAD",
               "-p", "MERGE_HEAD", "-p", staged_commit, "-m",
               f"backup: abandoned merge resolution ({task_id})"], cwd=wt)


CRASH_TAIL_LINES = 20


def _killed_by_signal(out, timed_out):
    code = getattr(out, "exit_code", 0)
    return not timed_out and code is not None and (code < 0 or code >= 128)


def _crashed(project, conn, run_id, out):
    summary = f"implementer killed by a signal (exit {out.exit_code})"
    print(f"[holo2] {summary}")
    if conn is not None and run_id is not None:
        text = redact_prose(out.strip(), known_secrets(project.config()))
        store.record_event(conn, run_id, "crash", summary, level="detail",
                           payload=json.dumps({
                               "exit_status": out.exit_code,
                               "output": "\n".join(
                                   text.splitlines()[-CRASH_TAIL_LINES:])}))


def _retry_crashed(project, conn, run_id, beat_s, wt, branch, task_id, goal,
                   out, deadline, budget_min, limit, start_sha):
    _crashed(project, conn, run_id, out)
    head, _, subject = sh(["git", "log", "-1", "--format=%H %s"],
                          cwd=wt).partition(" ")
    wip = (head if head != start_sha
           and subject == _wip_subject("crashed", task_id) else None)
    note = (f"Your previous turn on this task was killed by a signal (exit"
            f" {out.exit_code}) before it finished."
            + (f" A WIP commit {wip[:12]} on {branch} holds the edits it had"
               " not committed; build on it." if wip else "")
            + " Continue the task and commit your work.")
    remaining = deadline - retry_clock()
    _check_run_cap(project, conn, run_id, remaining / (budget_scale(project) * 60),
                   head)
    argv, _ = resume_argv(project, conn, run_id)
    out, timed_out = _transport_timed(
        project, conn, run_id, beat_s, wt, budget_min,
        note if argv is not None else f"{note}\n\n{goal}", argv=argv,
        seconds=remaining, limit=limit)
    if _killed_by_signal(out, timed_out):
        _crashed(project, conn, run_id, out)
        head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
        raise InfraFailure(f"implementer crashed twice (exit {out.exit_code});"
                           f" work kept on {branch} at {head[:12]}")
    return out, timed_out


def _failed_wip_only(wt, out, timed_out, start_sha, task_id):
    return (_turn_end(out, timed_out) == "failed"
            and sh(["git", "log", "-1", "--format=%P %s"], cwd=wt)
            == f"{start_sha} {_wip_subject('failed', task_id)}")


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
    seconds, limit = implement_arming(project, conn, run_id, budget_min)
    deadline = retry_clock() + seconds
    out, timed_out = _transport_timed(project, conn, run_id, beat_s, wt,
                                      budget_min, goal, seconds=seconds,
                                      limit=limit)
    if _killed_by_signal(out, timed_out):
        out, timed_out = _retry_crashed(project, conn, run_id, beat_s, wt,
                                        branch, task_id, goal, out, deadline,
                                        budget_min, limit, start_sha)
    blast_radius.record_declared(conn, run_id, out)
    boundary(conn, run_id, "verifying", unreproduced=reproduce.declared(out))
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    added = head != start_sha and not _failed_wip_only(wt, out, timed_out,
                                                       start_sha, task_id)
    # A reused branch already ahead of main is the candidate, even if the
    # turn adds nothing; it still owes verify and review.
    carried = not fresh and bool(
        subprocess.run(["git", "diff", "--quiet", "main", "HEAD"],
                       cwd=wt, capture_output=True).returncode)
    if not added and not carried:
        print(f"[holo2] implementer made no commits for: {task}")
        _record_implementer_output(conn, run_id, out,
                                   known_secrets(project.config()))
        fired = (f"; the turn exceeded the "
                 f"{_limit_text(project, limit, budget_min)}" if timed_out else "")
        if fresh:
            sh(["git", "worktree", "remove", "--force", str(wt)], project.path)
            sh(["git", "branch", "-D", branch], project.path)
            raise RunFailure("implementer made no commits; the empty branch"
                             f" and worktree were discarded{fired}",
                             "budget" if timed_out else "no_commits")
        raise RunFailure(f"implementer made no new commits; preserved work"
                         f" kept on {branch} at {head[:12]}{fired}",
                         "budget" if timed_out else "no_commits")
    if head == start_sha:
        note = (f"candidate carried from a prior run; implementer added"
                f" nothing to {branch} at {start_sha[:12]}")
        print(f"[holo2] {note}")
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "carried_candidate", note)
    if timed_out:
        raise RunFailure(f"implementer exceeded the "
                         f"{_limit_text(project, limit, budget_min)}; work kept"
                         f" on {branch} at {head[:12]}", "budget")
    return sh(["git", "rev-parse", "HEAD"], cwd=wt), reproduce.declared(out)
