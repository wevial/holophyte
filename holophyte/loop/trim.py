"""The trim step: one turn that shrinks the diff, kept only while verify is green."""
import json
import shutil
import subprocess
from functools import partial
from pathlib import Path

import store
from holophyte.config.config_tables import trim_config
from holophyte.loop.claim import merge_conflicts
from holophyte.loop.gates import InfraFailure, run_verify, sh, with_baseline
from holophyte.loop.implement import (
    OUTPUT_TAIL,
    _commands_brief,
    _killed_by_signal,
    _timed,
    run_cap_reason,
)
from holophyte.loop.review_round import _changed_lines
from holophyte.loop.runs import heartbeat_while
from holophyte.loop.trim_brief import TRIM_BRIEF
from holophyte.redact import known_secrets, redact_prose
from holophyte.redact import safe_print as print

PASSES = ("delete", "merge", "flatten", "comments", "tests")
LINE_FLOOR = 50


def trim(project, conn, run_id, beat_s, wt, base_sha, sha, verify_cmd,
         contracts):
    config = trim_config(project)
    if not config.enabled:
        return sha
    before = _changed_lines(wt)
    green = partial(_green, project, conn, run_id, beat_s, wt, verify_cmd,
                    contracts)
    reason = _skip_reason(project, conn, run_id, wt, config.budget_min, sha,
                          before, green)
    if reason:
        _record(project, conn, run_id, "skipped", reason, [], [], before, before)
        return sha
    untracked = _untracked(wt)
    goal = TRIM_BRIEF.format(base=base_sha,
                             commands=_commands_brief(project, verify_cmd).strip())
    out, reason = _turn(project, conn, run_id, beat_s, wt, config.budget_min, goal)
    _land(wt, "HEAD", untracked)
    commits = _commits(wt, sha)
    reason = reason or _malformed(wt, sha, commits)
    kept = [] if reason else _green_prefix(wt, commits, green, untracked)
    dropped = commits[len(kept):]
    if dropped and not reason:
        reason = f"verify was red at trim: {_passes(dropped)[0]['pass']}"
    _land(wt, kept[-1][0] if kept else sha, untracked)
    outcome = ("partial" if kept and dropped else "kept" if kept
               else "reverted" if reason else "nothing")
    _record(project, conn, run_id, outcome, reason, kept, dropped, before,
            _changed_lines(wt), out)
    return sh(["git", "rev-parse", "HEAD"], cwd=wt)


def _skip_reason(project, conn, run_id, wt, budget_min, sha, lines, green):
    if merge_conflicts(wt):
        return "the worktree is mid-merge with main"
    if lines < LINE_FLOOR:
        return f"diff of {lines} changed lines is under {LINE_FLOOR}"
    if not green():
        return "verify is red at the implementer's head"
    if run_cap_reason(project, conn, run_id, budget_min, sha):
        return f"the run cap would refuse a {budget_min} min turn"
    return None


def _turn(project, conn, run_id, beat_s, wt, budget_min, goal):
    try:
        out, timed_out = _timed(project, conn, run_id, beat_s, wt, budget_min,
                                goal, role="trim")
    except InfraFailure as error:
        return (getattr(error, "output", "") or str(error),
                f"the route failed: {error}")
    if timed_out:
        return out, "the turn timed out"
    if _killed_by_signal(out, timed_out):
        return out, f"the turn was killed by a signal (exit {out.exit_code})"
    if getattr(out, "exit_code", 0):
        return out, f"the turn exited with status {out.exit_code}"
    return out, None


def _green(project, conn, run_id, beat_s, wt, verify_cmd, contracts):
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts, conn=conn,
                             run_id=run_id, project=project)
        ok, _ = with_baseline(project, wt, verify_cmd, ok, out, conn, run_id)
    return ok


def _green_prefix(wt, commits, green, untracked):
    if not commits or green():
        return commits
    kept = []
    for commit in commits[:-1]:
        _land(wt, commit[0], untracked)
        if not green():
            break
        kept.append(commit)
    return kept


def _untracked(wt):
    return set(sh(["git", "ls-files", "-z", "--others"], cwd=wt).split("\0")) - {""}


def _land(wt, target, untracked):
    lock = Path(wt, sh(["git", "rev-parse", "--git-path", "index.lock"], cwd=wt))
    lock.unlink(missing_ok=True)
    # Mixed first: a file only the dropped commits track turns untracked, so
    # one that was untracked before the turn survives the hard reset.
    sh(["git", "reset", "-q", target], cwd=wt)
    sh(["git", "reset", "-q", "--hard"], cwd=wt)
    for name in _untracked(wt) - untracked:
        path = Path(wt, name)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def _commits(wt, sha):
    log = sh(["git", "log", "--first-parent", "--reverse", "--format=%H %s",
              f"{sha}..HEAD"], cwd=wt)
    return [line.partition(" ")[::2] for line in log.splitlines()]


def _malformed(wt, sha, commits):
    if subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                      cwd=wt, capture_output=True).returncode:
        return f"the turn rewrote history below {sha[:12]}"
    merges = sh(["git", "rev-list", "--merges", f"{sha}..HEAD"], cwd=wt)
    if merges:
        return f"the turn made a merge commit {merges.split()[0][:12]}"
    seen = set()
    for _, subject in commits:
        name = subject.removeprefix("trim: ")
        if name not in PASSES or subject != f"trim: {name}":
            return f"subject {subject!r} is not a trim pass"
        if name in seen:
            return f"pass {name!r} appears twice"
        seen.add(name)
    return None


def _passes(commits):
    return [{"pass": subject.removeprefix("trim: "), "sha": sha}
            for sha, subject in commits]


def _record(project, conn, run_id, outcome, reason, kept, dropped, before,
            after, out=""):
    kept, dropped = _passes(kept), _passes(dropped)
    summary = "; ".join(filter(None, (
        f"trim {outcome}",
        kept and "kept " + ", ".join(p["pass"] for p in kept),
        dropped and "reverted " + ", ".join(p["pass"] for p in dropped),
        reason,
        f"line delta {after - before:+d}")))
    print(f"[holo2] {summary}")
    if conn is None or run_id is None:
        return
    store.record_event(conn, run_id, "trim", summary)
    reply = redact_prose(str(out or "").strip(), known_secrets(project.config()))
    store.record_event(conn, run_id, "trim_result", summary, level="detail",
                       payload=json.dumps({
                           "outcome": outcome, "reason": reason, "kept": kept,
                           "reverted": dropped, "lines_before": before,
                           "lines_after": after,
                           "reply": reply[-OUTPUT_TAIL:]}))
