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

A ticket with a `## Reproduce` section is a bug ticket, and `first_turn()`
asks before any fix exists: a reproduce turn on the implementer seat, a third
of the estimate (3 to 10 minutes), commits a failing test only, and the
ticket's verify runs at that commit. A failure is a `reproduced` event and a
`Reproduction` whose `opening()` leads the implement turn built on it; a pass
sends the test commit straight to `review_rounds()` above, with no implement
turn; no commit is a ledger note and the implement turn as before (KO-659).
Whatever the turn leaves uncommitted is discarded either way, so neither the
verify nor the implement turn sees a half-written test or fix.

The loop's `agent`, `_timed`, `_check_run_cap`, `_verify_brief` and
`_review_rounds` are read off `holophyte.loop` at call time, so a test that
patches the loop's `agent` answers these turns too.
"""
import json
from dataclasses import dataclass, fields, replace
from pathlib import Path
from time import time

import review_runner
import store
import store.read
import ticket_template
from holophyte import failure_reason
from holophyte.agents import review_refs
from holophyte.board import block_ticket, ledger
from holophyte.environment_git import paths, unstage_environment
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
# The reproduce turn's budget bounds, in minutes (KO-659).
FIRST_MIN, FIRST_MAX = 3, 10

REDECLARE = ("If, once the tests exercise the reported path, the defect "
             "still does not reproduce, commit the tests only and end your "
             f"reply with exactly this line:\n{DECLARATION}")


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


@dataclass(frozen=True)
class Reproduction:
    """The reproduce turn's test commit and the verify command it made fail,
    None when the ticket's verify still passed there."""

    sha: str
    failing: object = None

    def opening(self):
        """The implement brief's first line for a reproduced defect."""
        return (f"A reproduce turn committed a test at {self.sha} that makes "
                f"the ticket's verify fail at `{self.failing}`. Build the fix "
                "on top of that commit and keep the test.\n\n")


def first_turn(target, conn, run_id, provider, task_id, wt, beat_s, start_sha,
               ticket, body, verify_cmd, budget_min):
    """A bug ticket's reproduce turn and the verify at its commit, or None:
    no `## Reproduce` section or verify to run it with, or no commit."""
    from holophyte import loop

    if not verify_cmd or "Reproduce" not in ticket_template.parse(body).order:
        return None
    budget = min(FIRST_MAX, max(FIRST_MIN, budget_min / 3))
    loop._check_run_cap(target, conn, run_id, budget, start_sha)
    loop._timed(
        target, conn, run_id, beat_s, wt, budget,
        "Reproduce the defect this ticket reports; do not fix it:\n\n"
        f"{ticket}\n\nThe ticket's verify commands:\n\n{verify_cmd}\n\n"
        "Write the smallest test that shows the reported behaviour, where "
        "those commands run it, and commit it. Change no application code: "
        "the fix is a later turn's. Commit messages carry no tool attribution"
        " or co-author lines for an AI.")
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    _discard_leftovers(target, wt)
    if head == start_sha:
        ledger(conn, run_id, task_id, "note",
               "No reproduction was committed: the reproduce turn added no "
               "commit, so the implement turn starts from the ticket alone "
               "on a tree with the turn's uncommitted edits discarded.",
               provider)
        return None
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, conn=conn, run_id=run_id,
                             target=target)
    if ok:
        print(f"[holo2] verify passes at the reproduce commit {head[:12]}")
        return Reproduction(head)
    failing = (getattr(out, "failure", None) or {}).get("command") or verify_cmd
    summary = f"reproduced at {head[:12]}: `{failing}` fails"
    print(f"[holo2] {summary}")
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "reproduced", summary, level="detail",
                           payload=json.dumps({"sha": head, "command": failing,
                                               "output": str(out)[-2000:]}))
    return Reproduction(head, failing)


def _discard_leftovers(target, wt):
    """Drop what the reproduce turn left uncommitted, so the verify at its
    commit and the implement turn after it see its commit alone. Only a
    commit is a reproduction; a half-written test or fix is not kept. The
    protected `.env` is unstaged first and excluded from the clean, and an
    `index.lock` a budget kill left behind can only be the dead turn's."""
    lock = Path(wt, sh(["git", "rev-parse", "--git-path", "index.lock"],
                       cwd=wt))
    lock.unlink(missing_ok=True)
    unstage_environment(target, wt)
    sh(["git", "reset", "-q", "--hard", "HEAD"], cwd=wt)
    sh(["git", "clean", "-fdq", *paths(target)], cwd=wt)


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
                             target=frame.target)
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
