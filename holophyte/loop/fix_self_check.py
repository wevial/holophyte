"""A fix turn's diff checked before the next review, one more turn on a failure."""
import os
import re

import store
from holophyte.agents.fix_session import self_check_turn
from holophyte.loop.follow_ups import _commits
from holophyte.loop.gates import sh
from holophyte.loop.implement import _check_run_cap, _timed
from holophyte.loop.importing_tests import run_importing_tests, unittest_runs
from holophyte.loop.runs import heartbeat_while
from holophyte.redact import safe_print as print
from holophyte.review.briefs import _changed_files
from holophyte.review.reply_parsing import FINDING_PATH_RE

_ADDRESS = re.compile(r"(?:[-*] )?ADDRESS:(.*)")


def _unaddressed(wt, candidate):
    files = sh(["git", "ls-files"], cwd=wt).splitlines()
    touched = {os.path.dirname(path)
               for path in _changed_files(wt, candidate, "HEAD")}
    for _, message in _commits(wt, candidate):
        for line in map(str.strip, message.splitlines()):
            match = _ADDRESS.fullmatch(line)
            cited = match and FINDING_PATH_RE.search(match.group(1))
            if not cited:
                continue
            path = cited.group(1).removeprefix("./")
            found = [file for file in files
                     if file == path or file.endswith(f"/{path}")]
            if found and not any(os.path.dirname(file) in touched
                                 for file in found):
                yield (f"`{line}` cites `{path}`, but the fix changes no file"
                       f" in its directory")


def _failfast(word):
    if word in ("-f", "--failfast"):
        return True
    flags = re.match(r"-([a-z]+)", word) if word[1:2] != "-" else None
    return bool(flags) and "f" in re.split("[pstk]", flags.group(1))[0]


def _executed(verify_cmd, passed, out):
    failed = (getattr(out, "failure", None) or {}).get("command") or ""
    if passed or not failed or failed not in verify_cmd:
        return verify_cmd if passed else ""
    end = verify_cmd.index(failed)
    if not any(map(_failfast, (word for run in unittest_runs(failed)
                               for word in run.args))):
        end += len(failed)
    return verify_cmd[:end].rstrip().removesuffix("&&").rstrip()


def _failures(project, conn, run_id, beat_s, wt, candidate, verify_cmd,
              checks):
    ok, out = checks()
    failures = [] if ok else [f"verify failed:\n{out}"]
    passed = {row.get("source") for row in getattr(out, "results", ())
              if row.get("exitCode") == 0}
    with heartbeat_while(conn, run_id, beat_s):
        ok, tests = run_importing_tests(
            project, wt, verify_cmd, conn, run_id, base=candidate,
            after_importers="importers" in passed,
            executed=_executed(verify_cmd, "ticket" in passed, out))
        unaddressed = list(_unaddressed(wt, candidate))
    return failures + ([] if ok else [tests["output"]]) + unaddressed


def _record(conn, run_id, summary):
    if conn is not None:
        store.record_event(conn, run_id, "fix_self_check", summary)


def after_fix(project, conn, run_id, beat_s, wt, budget_min, ticket, candidate,
              sha, verify_cmd, checks):
    """Check the fix diff `candidate..sha`; on a failure, take one more fix
    turn; return the head the next round reviews."""
    if sha == candidate:
        return sha
    failures = _failures(project, conn, run_id, beat_s, wt, candidate,
                         verify_cmd, checks)
    if not failures:
        print(f"[holo2] fix self-check passed at {sha[:12]}")
        _record(conn, run_id, f"fix self-check passed at {sha[:12]}")
        return sha
    turn = "never started"
    try:
        _check_run_cap(project, conn, run_id, budget_min, sha)
        turn = "made no commit"
        self_check_turn(project, conn, run_id, beat_s, wt, budget_min, ticket,
                        sha, failures, timed=_timed, check_cap=_check_run_cap)
    finally:
        head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
        turn = "committed" if head != sha else turn
        _record(conn, run_id,
                f"fix self-check at {sha[:12]}: {len(failures)} failures; the "
                f"extra turn {turn}, head {head[:12]}\n" + "\n".join(failures))
    return head
