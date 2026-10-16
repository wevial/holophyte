"""Witness pre-check gaps sent back to the implementer before the round 1 review."""
import store
import store.read
from holophyte.agents.fix_session import witness_turn
from holophyte.board.projection import ledger
from holophyte.loop.implement import _check_run_cap, _timed
from holophyte.loop.runs import set_phase
from holophyte.loop.stop import boundary
from holophyte.redact import safe_print as print
from holophyte.review import witness_precheck
from holophyte.review.briefs import witness_brief

WITNESS_TURNS = 2


def _gaps(entries):
    return [gap for entry in entries or () for gap in entry["gaps"]]


def _unnamed(entries):
    return [entry["criterion"] for entry in entries or ()
            if entry["named"] == "no_file"]


def _continued(conn, run_id):
    runs = [run_id]
    for earlier, outcome in conn.execute(
            "SELECT old.id, old.outcome FROM runs old JOIN runs current"
            " ON old.ticketId = current.ticketId WHERE current.id = ?"
            " AND old.attempt < current.attempt ORDER BY old.attempt DESC",
            (run_id,)).fetchall():
        if outcome != "paused":
            break
        runs.append(earlier)
    return runs


def _turns_taken(conn, run_id):
    return sum(event.kind == "witness_turn" for run in _continued(conn, run_id)
               for event in store.read.narrative_events(conn, run))


def _record_turn(conn, run_id, provider, task_id, turn, sha, gaps):
    store.record_event(conn, run_id, "witness_turn",
                       f"witness turn {turn} of {WITNESS_TURNS} at "
                       f"{sha[:12]}: {len(gaps)} gaps\n" + "\n".join(gaps))
    ledger(conn, run_id, task_id, "note",
           f"Witness pre-check: turn {turn} of {WITNESS_TURNS} before round 1",
           provider)


def _record_idle(conn, run_id, turn, gaps):
    text = (f"witness turn {turn} made no commit; review goes ahead with "
            f"{len(gaps)} gaps")
    print(f"[holo2] {text}")
    store.record_event(conn, run_id, "witness_turn_idle", text)


def before_review(project, conn, run_id, provider, task_id, beat_s, wt,
                  base_sha, sha, ticket, verify_cmd, criteria, budget_min, ok,
                  out, checks):
    """Turn and recheck until no gap, the cap, an idle turn or a red verify;
    return the head, its verify and the round 1 reviewer's brief."""
    entries = witness_precheck.record(project, conn, run_id, beat_s, wt,
                                      base_sha, sha, criteria, verify_cmd, ok)
    while ok and _gaps(entries):
        turn = _turns_taken(conn, run_id) + 1
        if turn > WITNESS_TURNS:
            break
        gaps = _gaps(entries)
        _check_run_cap(project, conn, run_id, budget_min, sha)
        boundary(conn, run_id, "verifying", rnd=1)
        _record_turn(conn, run_id, provider, task_id, turn, sha, gaps)
        set_phase(conn, run_id, "verifying",
                  f"round 1: witness turn {turn} of {WITNESS_TURNS}")
        head = witness_turn(project, conn, run_id, beat_s, wt, budget_min,
                            ticket, sha, gaps, timed=_timed,
                            check_cap=_check_run_cap)
        if head == sha:
            _record_idle(conn, run_id, turn, gaps)
            break
        sha = head
        ok, out = checks()
        entries = witness_precheck.record(project, conn, run_id, beat_s, wt,
                                          base_sha, sha, criteria, verify_cmd,
                                          ok)
    return sha, ok, out, witness_brief(_gaps(entries), _unnamed(entries))
