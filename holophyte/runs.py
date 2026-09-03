"""The store seam: a run's progress as store rows.

Every store call the loop makes goes through one of the five helpers here --
`open_store()` opens and migrates the store, `set_phase()` is the loop's single
writer of `runs.phase`, `heartbeat_while()` keeps the run's heartbeat moving
while the loop waits on an agent, `record_round()` turns a review or
adjudication reply into a `reviewRounds` row, and `warn_on_run()` lands a
best-effort failure in the run's event stream -- so a later wiring ticket
extends this seam instead of threading SQL through `run_task()`.
`MAX_ROUNDS`, the review-round ceiling the loop iterates to and the
adjudication round is numbered past, lives beside them. Beyond the standard
library it imports `store` for the writes,
`review_runner` for the verdict vocabularies, `agent_route` from
`holophyte.agents` for the route a round is stamped with, and the findings
parsers from `holophyte.review`.

Fifth slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import threading
from contextlib import contextmanager
from pathlib import Path
from time import time

import review_runner
import store
from holophyte.agents import agent_route
from holophyte.review import (
    criteria_findings,
    parse_findings,
    raw_finding,
    round_verdict,
)

MAX_ROUNDS = 2

# --- store seam --------------------------------------------------------------
# Every store call the loop makes goes through one of the helpers below, so a
# later wiring ticket extends this seam instead of threading SQL through
# run_task().


def open_store(target, path=None):
    """Open the loop's store, creating and migrating the schema if needed.

    The store's directory is made here, on first need: `Target.locate()` only
    derives paths, and a `--report` against a target that has no store says
    so without leaving an empty directory behind.
    """
    path = Path(path or target.store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = store.open(str(path))
    store.init(conn)
    return conn


def set_phase(conn, run_id, phase, note=None):
    """Record a stage boundary: the run's phase, its heartbeat, one event.

    The loop's single writer of `runs.phase` (state-model §6), which is why
    every stage below calls it instead of writing its own row: phase,
    heartbeat and narrative event move together in the store or not at all, so
    a crashed loop leaves a run parked where it stopped rather than parked in
    whatever phase it was last seen entering.

    A `conn` of None makes this a no-op, for `run_task()` driven directly
    without a store. That keeps one set of call sites rather than a storeless
    copy of the loop, and the phases are then simply not recorded.
    """
    if conn is None:
        return
    store.set_phase(conn, run_id, phase, note)


@contextmanager
def heartbeat_while(conn, run_id, interval_s):
    """Beat run `run_id`'s heartbeat every `interval_s` seconds inside the block.

    The loop blocks for as long as an agent or a verify command takes, and
    `set_phase()` moves `lastHeartbeat` only at stage boundaries, so a stage
    longer than the supervisor's stale threshold read as a dead loop and was
    swept while its `claude -p` was still working (KO-212, run 39). This
    wraps each such wait: a daemon thread calls `store.heartbeat()` on the
    interval, and the sweep's contract -- a dead heartbeat means a dead
    worker -- is true again. Same shape in any runtime: a timer thread and
    one UPDATE.

    The thread opens its own connection to the store `conn` is on, because a
    SQLite connection belongs to the thread that made it and the loop's
    `conn` is mid-use for the whole block. A beat that fails is printed as
    `[holo2] heartbeat failed: ...` and the block goes on: the agent's work
    is not lost to a locked store. On exit the thread is signalled and joined
    before the loop's next phase write, so no beat lands after the stage the
    block was for. A `conn` or `run_id` of None makes this a no-op, like
    `set_phase()`, for a storeless `run_task()`.
    """
    if conn is None or run_id is None:
        yield
        return
    (path,) = [row[2] for row in conn.execute("PRAGMA database_list")
               if row[1] == "main"]
    stop = threading.Event()

    def beat():
        try:
            own = store.open(path)
        except Exception as e:  # noqa: BLE001 - best effort; never the run's
            print(f"[holo2] heartbeat failed: {e}")
            return
        try:
            while not stop.wait(interval_s):
                try:
                    store.heartbeat(own, run_id)
                except Exception as e:  # noqa: BLE001 - same
                    print(f"[holo2] heartbeat failed: {e}")
        finally:
            own.close()

    thread = threading.Thread(target=beat, name=f"heartbeat-run-{run_id}",
                              daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def record_round(target, conn, run_id, rnd, role, reply, verify_cmd, ok, out,
                 started_at=None, criteria=()):
    """Record one review or adjudication round as a `reviewRounds` row.

    The round the loop just ran, as the store holds it: the verdict, the
    reviewer route that issued it, the verify result the reviewer was briefed
    with, and the findings — structured where the reply let them be extracted.
    `store.record_review_round()` fingerprints them on the way in, which is
    what makes two rounds comparable and is the whole reason the prose in
    FINDINGS.md is not enough.

    Which findings a round carries follows the round's kind. A `review` round
    that asked for changes carries what parsed out of its reply; an approval
    carries none, because approving prose is not a findings list. An
    adjudication is a verdict and nothing else by its own prompt, so PASS and
    FAIL both store an empty list — a reply that named no verdict at all is
    the exception, and its raw text is kept as the one finding rather than
    recorded as a round that said nothing.

    `criteria` are the ticket's acceptance criteria, in the order the
    reviewer was given them. A `review` round that leaves any of them `not
    met` or `unwitnessed` — or omits the checklist for a ticket that has
    criteria — is recorded as `changes_requested` with one finding per such
    criterion, even when its verdict line says APPROVE: the verdict line is
    still read as before, and the override is applied after it. The
    adjudicator keeps its bare PASS/FAIL contract and is not held to the
    checklist.

    A `conn` of None makes this a no-op, like `set_phase()`, so a storeless
    `run_task()` runs the same stages and records nothing.
    """
    if conn is None:
        return
    verdicts = (review_runner.REVIEW_VERDICTS if role == "review"
                else review_runner.ADJUDICATION_VERDICTS)
    verdict = round_verdict(reply, verdicts)
    if verdict == "error":
        findings = [raw_finding(reply)]
    elif verdict == "changes_requested" and role == "review":
        findings = parse_findings(reply)
    else:
        findings = []
    if role == "review" and verdict != "error":
        unwitnessed = criteria_findings(reply, criteria)
        if unwitnessed:
            verdict = "changes_requested"
            findings = findings + unwitnessed
    # `run_verify()` reports a pass/fail gate rather than a raw status — the
    # failing clause and its exit code live in the output it builds — so the
    # exit code stored here is that verdict, and `output` is the detail.
    results = ([{"command": verify_cmd, "exitCode": 0 if ok else 1,
                 "output": out}] if verify_cmd else [])
    store.record_review_round(conn, run_id, rnd, verdict,
                              agent_route(target, role),
                              findings=findings, verification_results=results,
                              started_at=started_at,
                              ended_at=int(time() * 1000))


def warn_on_run(conn, run_id, summary):
    """Print a warning and record it against `run_id`; never raise.

    The half of `warn()` that a caller already holding a run id uses directly.
    Best-effort work that failed is still part of the run's account of itself,
    so it lands in the same event stream as the phase changes rather than only
    on stdout. A missing store or run leaves the printed line as the whole
    record, which is the same no-op `set_phase()` makes for a storeless
    `run_task()`.
    """
    print(f"[holo2] {summary}")
    if conn is None or run_id is None:
        return
    store.record_event(conn, run_id, "warning", summary)
