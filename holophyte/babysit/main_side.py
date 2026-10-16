from typing import NamedTuple

import store
from holophyte.babysit.main_checkout import detached_main
from holophyte.loop.gates import drop_candidate_modules, sh


class MainSide(NamedTuple):
    sha: str
    ok: object
    out: object
    command: str
    state: str


def verify_detached_main(project, conn, run_id, beat_s, wt, ref, command,
                         contracts, failure, refresh_verify):
    sha = sh(["git", "rev-parse", ref], wt)
    with detached_main(project, conn, run_id, beat_s, wt, sha) as (
            detached, setup_failure):
        if setup_failure is not None:
            return MainSide(sha, None, setup_failure, command,
                            "was not verified because its worktree setup"
                            f" failed:\n{setup_failure}\n")
        side = drop_candidate_modules(command, wt, detached, sha)
        _record_skipped(conn, run_id, sha, side)
        failing = (failure or {}).get("command", "").strip()
        if failing in side.dropped:
            named = ", ".join(side.dropped[failing])
            _record(conn, run_id,
                    f"main-side verify at {sha[:12]} not comparable: the failing"
                    f" command {failing} names {named}, which only the"
                    " candidate adds")
            return MainSide(sha, None, None, side.command,
                            f"was not compared: the failing command names {named},"
                            " which only this branch adds. ")
        ok, out = refresh_verify(project, conn, run_id, beat_s, detached, sha,
                                 side.command, contracts)
    trimmed = side.trimmed.get(failing)
    state = (f"passes without {', '.join(trimmed)}, which only this branch"
             " adds. " if trimmed else "passes. ")
    return MainSide(sha, ok, out, side.command, state)


def _record_skipped(conn, run_id, sha, side):
    names = side.modules + side.paths
    if names:
        reason = " or ".join(verb for verb, found in (
            ("import", side.modules), ("run", side.paths)) if found)
        _record(conn, run_id,
                f"main-side verify at {sha[:12]} skipped {', '.join(names)}:"
                f" exists only on the candidate, not on main, so main cannot"
                f" {reason} it")


def _record(conn, run_id, summary):
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "verification", summary)
