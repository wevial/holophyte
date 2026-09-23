"""A reported defect the implementer could not reproduce, put to the maintainer.

The implement brief (`BRIEF`) lets an implementer whose faithful test passes
on the unchanged code say so: it commits the test(s) only and ends its reply
with `DECLARATION`. `review_rounds()` then stands in for the loop's
`_review_rounds()` for round 1: verify as usual, then one evidence check on
the adjudicate seat instead of a review -- do the added tests exercise the
reported path, and is the candidate nothing but tests? PASS parks the run
`awaiting_merge_approval` with `parkKind = 'not_reproduced'` and the ticket
`blocked_on_operator`, asking which of three answers the maintainer gives.
`MergeParked` is neither a release nor a strike, so a report whose only
fault was its premise costs the ticket nothing.

Anything else hands back to `_review_rounds()`: a declaration whose verify
fails is set aside and round 1 is an ordinary review; a FAIL (or a reply
with no verdict line) is round 1 `changes_requested`, its reasons go to one
fix turn, and a fix turn that declares again gets one more check before an
ordinary round 2 (KO-657).

A pause anywhere on this route resumes on it: every checkpoint the run
saves once the route is taken carries `unreproduced` (and, once handed on,
the raised cap as `handed_on`), and `routed()` sends the continuation back
here. A storeless run takes the same route and parks by raising
`MergeParked` with nothing recorded.

The loop's `agent`, `_timed`, `_check_run_cap`, `_verify_brief` and
`_review_rounds` are read off `holophyte.loop` at call time, so a test that
patches the loop's `agent` answers these turns too.
"""
import json
from dataclasses import dataclass, fields, replace
from time import time

import review_runner
import store
import store.read
from holophyte import failure_reason
from holophyte.agents import review_refs
from holophyte.board import block_ticket, ledger
from holophyte.gates import MergeParked, RunFailure, run_verify, sh, with_baseline
from holophyte.redact import safe_print as print
from holophyte.review import raw_finding
from holophyte.runs import heartbeat_while, record_round, set_phase
from holophyte.stop import boundary, keep_route

DECLARATION = "OUTCOME: NOT_REPRODUCED"

BRIEF = ("\n\nIf the ticket reports a defect and a test built from the real "
         "data shape passes on the unchanged code, the defect did not "
         "reproduce: commit the test(s) only, change no other code, and end "
         f"your reply with exactly this line:\n{DECLARATION}")

# Appended to the evidence check's reasons for the one fix turn after a FAIL.
REDECLARE = ("If, once the tests exercise the reported path, the defect "
             "still does not reproduce, commit the tests only and end your "
             f"reply with exactly this line:\n{DECLARATION}")

# The first line of the pull request an approved `not_reproduced` candidate
# opens (KO-658): written by the loop, since the writer turn is given the
# ticket, whose title reports a bug.
TESTS_ONLY = ("Tests only: the reported behaviour did not reproduce on {base};"
              " these tests are kept as a regression guard.")


def tests_only_line(conn, run_id):
    """`TESTS_ONLY` for the carried run `run_id` when it parked
    `not_reproduced`, naming the base its park's event recorded; None for
    any other park or with no store. The event is the witness, not
    `runs.parkKind`: the ticket's walk out of `blocked_on_operator` on the
    approval clears that column, and only `_park_in_store()` records the
    event."""
    if conn is None:
        return None
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ?"
        " AND kind = 'not_reproduced' ORDER BY seq DESC LIMIT 1",
        (run_id,)).fetchone()
    base = json.loads(row[0])["base"] if row and row[0] else None
    return TESTS_ONLY.format(base=base) if base else None


def declared(reply):
    """Whether `reply`'s last non-empty line is the declaration."""
    lines = [line.strip() for line in str(reply or "").splitlines()
             if line.strip()]
    return bool(lines) and lines[-1] == DECLARATION


@dataclass(frozen=True)
class Frame:
    """`_review_rounds()`'s positional arguments, in its order."""

    target: object
    conn: object
    run_id: object
    provider: object
    task_id: str
    branch: str
    wt: object
    beat_s: float
    base_sha: str
    sha: str
    ticket: str
    verify_cmd: object
    contracts: object
    criteria: object
    budget_min: float
    cap: int

    def args(self):
        return tuple(getattr(self, field.name) for field in fields(self))


def routed(resume):
    """Whether a continuation resumes on this route."""
    return bool(resume and resume.get("unreproduced"))


def review_rounds(*args, resume=None):
    """`_review_rounds()` for a candidate declared not reproduced: park it on
    a passing evidence check, else hand back for ordinary rounds. `resume`
    is a checkpoint this route saved, and picks up where it paused."""
    from holophyte import loop

    frame, pending = Frame(*args), resume or {}
    if "handed_on" in pending:
        # Paused in the ordinary rounds this route had already handed on to.
        return _hand_on(loop, frame, pending, pending["handed_on"])
    keep_route(frame.conn, frame.run_id, unreproduced=True)
    if pending.get("phase") == "addressing":
        ok, out = _verified(frame, 1, pending)
        set_phase(frame.conn, frame.run_id, "reviewing",
                  "round 1: the evidence check's FAIL, kept from the pause")
        reasons = pending["verdict"]
    elif pending.get("rnd", 1) == 2:
        return _second(loop, frame, pending)
    else:
        ok, out = _verified(frame, 1, pending)
        if not ok:
            return _set_aside(loop, frame, 1, out)
        reply, decision, started = _check(loop, frame, 1, ok, out)
        _record(frame, 1, reply, decision, ok, out, started)
        if decision == "PASS":
            _park(frame)
        reasons = _reasons(reply)
    fixes, head = _fix(loop, frame, reasons, ok, out)
    return _second(loop, replace(frame, sha=head),
                   {"declared": declared(fixes)})


def _second(loop, frame, pending):
    """After round 1's fix turn: one more evidence check for a fix that
    declared again, an ordinary round 2 for one that did not."""
    if not pending.get("declared"):
        return _hand_on(loop, frame, {"rnd": 2})
    ok, out = _verified(frame, 2, pending)
    if not ok:
        return _set_aside(loop, frame, 2, out)
    reply, decision, started = _check(loop, frame, 2, ok, out)
    if decision == "PASS":
        _record(frame, 2, reply, decision, ok, out, started)
        _park(frame)
    # Round 2 belongs to the ordinary review this hands on to; the second
    # check's reply is kept on the run instead.
    _event(frame, "not_reproduced_refused",
           f"second evidence check: {decision}; round 2 is an ordinary review",
           reply)
    return _hand_on(loop, frame, {"phase": "reviewing", "rnd": 2, "ok": ok,
                                  "out": out})


def _hand_on(loop, frame, pending, floor=None):
    """`_review_rounds()` from `pending`, whose ordinary round the evidence
    check's round 1 must not take from a one-round cap: the cap rises to
    that round (or to `floor`, the cap a resumed hand-on had risen to), on
    the run too, and the loop numbers its terminal adjudication after the
    round it returns. Later checkpoints carry the risen cap."""
    cap = max(frame.cap, floor or pending["rnd"])
    if cap != frame.cap:
        frame = replace(frame, cap=cap)
        if frame.conn is not None and frame.run_id is not None:
            store.set_review_round_cap(frame.conn, frame.run_id, frame.cap)
    keep_route(frame.conn, frame.run_id, unreproduced=True, handed_on=cap)
    return loop._review_rounds(*frame.args(), resume=pending)


def _verified(frame, rnd, pending):
    """Round `rnd`'s verify at the declared candidate, or the result a pause
    after it kept -- the phase walked through either way."""
    boundary(frame.conn, frame.run_id, "verifying", rnd=rnd, declared=True)
    if pending.get("phase") in ("reviewing", "addressing"):
        set_phase(frame.conn, frame.run_id, "verifying",
                  f"round {rnd}: verify kept from the pause")
        return pending.get("ok", False), pending.get("out", "")
    return _verify(frame, rnd)


def _verify(frame, rnd):
    """The ticket's verify and the baseline at the declared candidate."""
    set_phase(frame.conn, frame.run_id, "verifying",
              f"round {rnd}: verify the not-reproduced declaration")
    with heartbeat_while(frame.conn, frame.run_id, frame.beat_s):
        ok, out = run_verify(frame.verify_cmd, frame.wt, frame.contracts,
                             conn=frame.conn, run_id=frame.run_id,
                             project=frame.target)
        ok, out = with_baseline(frame.target, frame.wt, frame.verify_cmd, ok,
                                out, frame.conn, frame.run_id)
    print(f"[holo2] verify {'ok' if ok else 'FAILED'} before round {rnd}")
    return ok, out


def _set_aside(loop, frame, rnd, out):
    """A declaration verify refused: note it, then an ordinary round `rnd`."""
    _event(frame, "not_reproduced_set_aside",
           f"not-reproduced declaration at {frame.sha[:12]} set aside: verify"
           f" failed; round {rnd} is an ordinary review", str(out))
    return _hand_on(loop, frame, {"phase": "reviewing", "rnd": rnd,
                                  "ok": False, "out": out})


def _check(loop, frame, rnd, ok, out):
    """One evidence-check turn; return its reply, decision and start time."""
    boundary(frame.conn, frame.run_id, "reviewing", rnd=rnd, ok=ok,
             out=str(out), declared=True)
    set_phase(frame.conn, frame.run_id, "reviewing",
              f"round {rnd}: not-reproduced evidence check")
    base, candidate = review_refs(frame.run_id)
    started = int(time() * 1000)
    with heartbeat_while(frame.conn, frame.run_id, frame.beat_s):
        reply = loop.agent(
            frame.target, "adjudicate",
            "You are a READ-ONLY evidence checker. The implementer of the "
            "ticket below could not reproduce the defect it reports: it "
            "committed tests only and declared the behaviour not reproduced. "
            f"Judge commit {frame.sha} using {base} (base {frame.base_sha}) "
            f"as the frozen base and {candidate} (candidate {frame.sha}) as "
            f"the candidate in this repo.\n\n{frame.ticket}\n\n"
            + loop._verify_brief(frame.verify_cmd, ok, out)
            + "Answer one question: do the tests the candidate adds exercise "
            "the path the ticket reports, and does the candidate change "
            "nothing but tests? Do not modify anything. Give your reasons, "
            "then end your reply with exactly one line:\n"
            "VERDICT: PASS  or  VERDICT: FAIL\n"
            "PASS means yes to both; the run then waits on the maintainer.",
            frame.wt, base_sha=frame.base_sha, candidate_sha=frame.sha,
            conn=frame.conn, run_id=frame.run_id)
    try:
        decision = review_runner.terminal_verdict(
            reply, review_runner.ADJUDICATION_VERDICTS)
    except review_runner.ReviewBoundaryError:
        decision = "MALFORMED"
    print(f"[holo2] round {rnd}: not-reproduced evidence check {decision}")
    return str(reply), decision, started


def _reasons(reply):
    """The check's reply without its verdict line."""
    lines = reply.rstrip().splitlines()
    if lines and lines[-1].strip().startswith("VERDICT:"):
        lines = lines[:-1]
    return "\n".join(lines).strip() or reply


def _record(frame, rnd, reply, decision, ok, out, started):
    """The check as round `rnd`; anything but PASS is `changes_requested`
    with its reasons as the finding -- no verdict line reads as FAIL."""
    findings = None if decision == "PASS" else [raw_finding(_reasons(reply))]
    verdict = reply if decision != "MALFORMED" else f"{reply}\nVERDICT: FAIL"
    record_round(frame.target, frame.conn, frame.run_id, rnd, "adjudicate",
                 verdict, frame.verify_cmd, ok, out, started_at=started,
                 structured_findings=findings)


def _fix(loop, frame, reasons, ok, out):
    """Round 1's fix turn on the check's reasons; return its reply and HEAD."""
    from holophyte.fix_session import fix_turn

    loop._check_run_cap(frame.target, frame.conn, frame.run_id,
                        frame.budget_min, frame.sha)
    boundary(frame.conn, frame.run_id, "addressing", rnd=1, ok=ok,
             out=str(out), verdict=reasons)
    set_phase(frame.conn, frame.run_id, "addressing",
              "round 1: addressing the evidence check")
    fixes, timed_out = fix_turn(
        frame.target, frame.conn, frame.run_id, frame.beat_s, frame.wt,
        frame.budget_min, frame.ticket, f"{reasons}\n\n{REDECLARE}",
        frame.sha, timed=loop._timed, check_cap=loop._check_run_cap)
    boundary(frame.conn, frame.run_id, "verifying", rnd=2,
             declared=declared(fixes))
    ledger(frame.conn, frame.run_id, frame.task_id, "round",
           "Round 1: not-reproduced evidence check FAIL -> fix round\n"
           f"Evidence check:\n{reasons}\n\nImplementer response:\n{fixes}",
           frame.provider)
    head = sh(["git", "rev-parse", "HEAD"], cwd=frame.wt)
    if timed_out or head == frame.sha:
        print(f"[holo2] fix round timed out or made no progress; leaving"
              f" branch {frame.branch} at {frame.sha} for a human.")
        rounds = (store.read.rounds_of(frame.conn, frame.run_id)
                  if frame.conn else [])
        pending = json.loads(rounds[-1].findings) if rounds else []
        raise RunFailure(failure_reason.fix_round(
            pending, timed_out,
            f"branch {frame.branch} preserved at {frame.sha[:12]}"))
    return fixes, head


def _event(frame, kind, summary, detail):
    print(f"[holo2] {summary}")
    if frame.conn is not None and frame.run_id is not None:
        store.record_event(frame.conn, frame.run_id, kind, summary,
                           level="detail",
                           payload=json.dumps({"detail": str(detail)[-2000:]}))


def _park(frame):
    """Park on the maintainer the way `_park_for_approval()` parks."""
    conn, run_id, task_id = frame.conn, frame.run_id, frame.task_id
    first = (f"not reproduced: tests at {frame.sha[:12]} pass on base"
             f" {frame.base_sha[:12]}")
    question = (
        f"{first}\nAnswer one of:\n"
        f"- cancel {task_id} on the board to close it;\n"
        f"- add detail to its body, then `--requeue {task_id} --note TEXT`;\n"
        f"- `--approve {task_id}` to keep the tests as a regression guard"
        " (a tests-only pull request under [merge] mode = \"pr\").")
    if conn is None or run_id is None:
        # A storeless run records nothing: the question is printed instead.
        print(f"[holo2] no store to park the run in:\n{question}")
    else:
        _park_in_store(frame, first, question)
    print(f"[holo2] {first}; parked {frame.branch} for the maintainer")
    ledger(conn, run_id, task_id, "note",
           f"NOT REPRODUCED: the evidence check passed; branch {frame.branch}"
           f" preserved at {frame.sha} and not merged.\n\n{question}",
           frame.provider)
    raise MergeParked(f"not reproduced; branch {frame.branch} preserved"
                      f" at {frame.sha[:12]}")


def _park_in_store(frame, first, question):
    """The park's store writes: the event, the ticket's question, the run."""
    conn, run_id = frame.conn, frame.run_id
    store.record_event(conn, run_id, "not_reproduced", first, level="detail",
                       payload=json.dumps({"base": frame.base_sha,
                                           "candidate": frame.sha}))
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    if not block_ticket(conn, ticket_id, frame.provider, question,
                        park_kind="not_reproduced"):
        print(f"[holo2] {frame.task_id} could not be moved to"
              " blocked_on_operator; parking the run anyway")
    store.park(conn, run_id, "awaiting_merge_approval",
               f"{first}; {frame.branch} waits on the maintainer",
               candidate_sha=frame.sha, park_kind="not_reproduced")
