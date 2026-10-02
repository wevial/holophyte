import json
import subprocess
from dataclasses import dataclass, fields, replace
from pathlib import Path
from time import time

import review_runner
import store
import store.read
import ticket_template
from holophyte import failure_reason
from holophyte.agents.review_workspace import review_refs
from holophyte.board.projection import block_ticket, ledger
from holophyte.config.reader import VERIFY_TIMEOUT
from holophyte.environment_git import paths, unstage_environment
from holophyte.loop.gates import (
    MergeParked,
    RunFailure,
    _verify_command,
    run_verify,
    sh,
    with_baseline,
)
from holophyte.loop.runs import heartbeat_while, record_round, set_phase
from holophyte.loop.stop import boundary, keep_route
from holophyte.redact import safe_print as print
from holophyte.review import skipped_tests
from holophyte.review.reply_parsing import raw_finding
from store.working import working

DECLARATION = "OUTCOME: NOT_REPRODUCED"

BRIEF = ("\n\nIf the ticket reports a defect and a test built from the real "
         "data shape passes on the unchanged code, the defect did not "
         "reproduce: commit the test(s) only, change no other code, and end "
         f"your reply with exactly this line:\n{DECLARATION}")

FIRST_MIN, FIRST_MAX = 3, 10

REDECLARE = ("If, once the tests exercise the reported path, the defect "
             "still does not reproduce, commit the tests only and end your "
             f"reply with exactly this line:\n{DECLARATION}")

TESTS_ONLY = ("Tests only: the reported behaviour did not reproduce on {base};"
              " these tests are kept as a regression guard.")


def tests_only_line(conn, run_id):
    if conn is None:
        return None
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ?"
        " AND kind = 'not_reproduced' ORDER BY seq DESC LIMIT 1",
        (run_id,)).fetchone()
    base = json.loads(row[0])["base"] if row and row[0] else None
    return TESTS_ONLY.format(base=base) if base else None


def declared(reply):
    lines = [line.strip() for line in str(reply or "").splitlines()
             if line.strip()]
    return bool(lines) and lines[-1] == DECLARATION


@dataclass(frozen=True)
class Frame:
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
    sha: str
    failing: object = None

    def opening(self):
        return (f"A reproduce turn committed a test at {self.sha} that makes "
                f"the ticket's verify fail at `{self.failing}`. Build the fix "
                "on top of that commit and keep the test.\n\n")


def first_turn(target, conn, run_id, provider, task_id, wt, beat_s, start_sha,
               ticket, body, verify_cmd, budget_min):
    from holophyte.loop import implement

    if not verify_cmd or "Reproduce" not in ticket_template.parse(body).order:
        return None
    budget = min(FIRST_MAX, max(FIRST_MIN, budget_min / 3))
    implement._check_run_cap(target, conn, run_id, budget, start_sha)
    try:
        implement._timed(
            target, conn, run_id, beat_s, wt, budget,
            "Reproduce the defect this ticket reports; do not fix it:\n\n"
            f"{ticket}\n\nThe ticket's verify commands:\n\n{verify_cmd}\n\n"
            "Write the smallest test that shows the reported behaviour, where "
            "those commands run it, and commit it. Change no application code:"
            " the fix is a later turn's. Commit messages carry no tool "
            "attribution or co-author lines for an AI.")
    finally:
        # Also when the turn raises: the next claim would commit these edits.
        _discard_leftovers(target, wt)
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if head == start_sha:
        ledger(conn, run_id, task_id, "note",
               "No reproduction was committed: the reproduce turn added no "
               "commit, so the implement turn starts from the ticket alone "
               "on a tree with the turn's uncommitted edits discarded.",
               provider)
        return None
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, conn=conn, run_id=run_id,
                             project=target)
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
    lock = Path(wt, sh(["git", "rev-parse", "--git-path", "index.lock"],
                       cwd=wt))
    lock.unlink(missing_ok=True)
    unstage_environment(target, wt)
    sh(["git", "reset", "-q", "--hard", "HEAD"], cwd=wt)
    sh(["git", "clean", "-fdq", *paths(target)], cwd=wt)


def routed(resume):
    return bool(resume and resume.get("unreproduced"))


def review_rounds(*args, resume=None):
    import holophyte.loop.review_round as loop

    frame, pending = Frame(*args), resume or {}
    if "handed_on" in pending:
        return _hand_on(loop, frame, pending, pending["handed_on"])
    keep_route(frame.conn, frame.run_id, unreproduced=True)
    headline = "not-reproduced evidence check FAIL"
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
        skipped = _skipped(frame)
        if skipped:
            headline = "new tests skipped on the base"
            set_phase(frame.conn, frame.run_id, "reviewing",
                      f"round 1: {headline}, no evidence check")
            reasons = skipped_tests.brief(skipped)
        else:
            reply, decision, started = _check(loop, frame, 1, ok, out)
            _record(frame, 1, reply, decision, ok, out, started)
            if decision == "PASS":
                _park(frame)
            reasons = _reasons(reply)
    fixes, head = _fix(loop, frame, reasons, ok, out, headline)
    return _second(loop, replace(frame, sha=head),
                   {"declared": declared(fixes)})


def _second(loop, frame, pending):
    if not pending.get("declared"):
        return _hand_on(loop, frame, {"rnd": 2})
    ok, out = _verified(frame, 2, pending)
    if not ok:
        return _set_aside(loop, frame, 2, out)
    if _skipped(frame):
        return _hand_on(loop, frame, {"phase": "reviewing", "rnd": 2, "ok": ok,
                                      "out": out})
    reply, decision, started = _check(loop, frame, 2, ok, out)
    if decision == "PASS":
        _record(frame, 2, reply, decision, ok, out, started)
        _park(frame)
    _event(frame, "not_reproduced_refused",
           f"second evidence check: {decision}; round 2 is an ordinary review",
           reply)
    return _hand_on(loop, frame, {"phase": "reviewing", "rnd": 2, "ok": ok,
                                  "out": out})


def _hand_on(loop, frame, pending, floor=None):
    cap = max(frame.cap, floor or pending["rnd"])
    if cap != frame.cap:
        frame = replace(frame, cap=cap)
        if frame.conn is not None and frame.run_id is not None:
            store.set_review_round_cap(frame.conn, frame.run_id, frame.cap)
    keep_route(frame.conn, frame.run_id, unreproduced=True, handed_on=cap)
    return loop._review_rounds(*frame.args(), resume=pending)


def _verified(frame, rnd, pending):
    boundary(frame.conn, frame.run_id, "verifying", rnd=rnd, declared=True)
    if pending.get("phase") in ("reviewing", "addressing"):
        set_phase(frame.conn, frame.run_id, "verifying",
                  f"round {rnd}: verify kept from the pause")
        return pending.get("ok", False), pending.get("out", "")
    return _verify(frame, rnd)


def _verify(frame, rnd):
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
    _event(frame, "not_reproduced_set_aside",
           f"not-reproduced declaration at {frame.sha[:12]} set aside: verify"
           f" failed; round {rnd} is an ordinary review", str(out))
    return _hand_on(loop, frame, {"phase": "reviewing", "rnd": rnd,
                                  "ok": False, "out": out})


def _skipped(frame):
    added = skipped_tests.added_tests(frame.wt, frame.base_sha, frame.sha)
    probe = added and skipped_tests.probe_command(frame.verify_cmd)
    skipped = probe and skipped_tests.skipped_on_base(_probe(frame, probe),
                                                      added)
    if not skipped:
        return None
    summary = ("the candidate's new tests did not run where the evidence "
               f"check runs: {', '.join(test for test, _ in skipped)}")
    print(f"[holo2] {summary}")
    if frame.conn is not None and frame.run_id is not None:
        store.record_event(frame.conn, frame.run_id, "reproduce_skipped",
                           summary, level="detail", payload=json.dumps(
                               {"skipped": [{"test": test, "reason": reason}
                                            for test, reason in skipped]}))
    return skipped


def _probe(frame, command):
    with working(frame.conn, frame.run_id, verify=True), \
            heartbeat_while(frame.conn, frame.run_id, frame.beat_s):
        try:
            return _verify_command(frame.target, command, frame.wt,
                                   VERIFY_TIMEOUT)[1] or ""
        except (OSError, subprocess.TimeoutExpired):
            return ""


def _check(loop, frame, rnd, ok, out):
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
    lines = reply.rstrip().splitlines()
    if lines and lines[-1].strip().startswith("VERDICT:"):
        lines = lines[:-1]
    return "\n".join(lines).strip() or reply


def _record(frame, rnd, reply, decision, ok, out, started):
    findings = None if decision == "PASS" else [raw_finding(_reasons(reply))]
    verdict = reply if decision != "MALFORMED" else f"{reply}\nVERDICT: FAIL"
    record_round(frame.target, frame.conn, frame.run_id, rnd, "adjudicate",
                 verdict, frame.verify_cmd, ok, out, started_at=started,
                 structured_findings=findings)


def _fix(loop, frame, reasons, ok, out, headline):
    from holophyte.agents.fix_session import fix_turn

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
           f"Round 1: {headline} -> fix round\n"
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
