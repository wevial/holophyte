"""The store seam: a run's progress as store rows.

Every store call the loop makes goes through one of the five helpers here --
`open_store()` opens and migrates the store, `set_phase()` is the loop's single
writer of `runs.phase`, `heartbeat_while()` keeps the run's heartbeat moving
while the loop waits on an agent, `record_round()` turns a review or
adjudication reply into a `reviewRounds` row, and `warn_on_run()` lands a
best-effort failure in the run's event stream -- so a later wiring ticket
extends this seam instead of threading SQL through `run_task()`.
`MAX_ROUNDS`, the default review-round base the loop iterates to and the
adjudication round is numbered past, lives beside them, with
`review_round_cap()`, the per-run ceiling computed from the candidate's size
and the `[loop]` review keys. Beyond the standard
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
import store.read
from holophyte.agents import agent_route
from holophyte.review import (
    criteria_findings,
    parse_findings,
    raw_finding,
    round_verdict,
)

MAX_ROUNDS = 2


def review_round_cap(changed_lines, cfg):
    """The number of review rounds a candidate of `changed_lines` earns.

    `cfg` is a `LoopConfig`: `review_rounds` is the base every run gets,
    `review_rounds_per_lines` buys one more round per that many changed
    lines (insertions plus deletions against the merge base; `0` turns the
    scaling off) and `review_rounds_max` is the ceiling. Two rounds fit the
    twenty-five-minute tickets that fill the queue; a two-thousand-line one
    (KO-262) found a real blocker in every round and ran out of rounds with
    its last fix unreviewed, so a big change earns its rounds and a small
    one keeps paying the base. Pure, so the formula is witnessed on its own
    and the loop only has to measure the diff.
    """
    extra = (changed_lines // cfg.review_rounds_per_lines
             if cfg.review_rounds_per_lines else 0)
    return min(cfg.review_rounds_max, cfg.review_rounds + extra)

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


class RunSwept(Exception):
    """The run `heartbeat_while()` was keeping alive was ended from outside.

    Raised when the block exits after a beat found the run's `endedAt`
    stamped: the supervisor's sweep -- or an operator's `--sweep --act` --
    failed the run while the loop was inside a turn (KO-339, run 160). Not a
    `store.RunEnded`: that one is a refused write the loop finds out about
    at its next phase change, while this one is the heartbeat noticing
    mid-turn, with the turn's process already killed through `on_swept`.
    `run_id` and `reason` are the ended row's, so the catcher can say what
    ended the run without reading the store again.
    """

    def __init__(self, run_id, outcome, reason):
        super().__init__(f"run {run_id} was swept ({outcome}: {reason})")
        self.run_id, self.outcome, self.reason = run_id, outcome, reason


@contextmanager
def heartbeat_while(conn, run_id, interval_s, on_swept=None):
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

    The beat also notices the run being ended from outside. `store.heartbeat()`
    answers False, and writes nothing, when the run's `endedAt` is stamped;
    the beat thread takes that answer as "swept", calls `on_swept` once (the
    implement turn's kill of its process group, so the agent stops working
    for a run the store has already failed -- run 160, KO-339, worked on for
    twenty minutes after its sweep), stops beating, and when the block exits
    `RunSwept` is raised naming the run and its recorded outcome reason,
    whether the body returned or raised. A body that ends on its own path
    inside a live run is unaffected: no beat fails, nothing is raised.

    The exit beats once more, on the caller's own `conn`, after the thread
    is joined. A run ended between the timer's last beat and the block's
    exit -- a sweep landing as the agent finishes -- was otherwise never
    seen, and the loop went on to verify and record against it. That last
    beat calls no `on_swept`: the turn it would have stopped has already
    returned. It only decides whether the block ends normally or raises.
    """
    if conn is None or run_id is None:
        yield
        return
    (path,) = [row[2] for row in conn.execute("PRAGMA database_list")
               if row[1] == "main"]
    stop = threading.Event()
    swept = []  # the ended row's (outcome, reason), set once by the beat
    thread = threading.Thread(
        target=_beat, args=(path, run_id, interval_s, stop, swept, on_swept),
        name=f"heartbeat-run-{run_id}", daemon=True)
    thread.start()
    failure = None
    try:
        yield
    except BaseException as e:  # noqa: BLE001 - re-raised below, after the join
        failure = e
    finally:
        stop.set()
        thread.join()
    if not swept and not store.heartbeat(conn, run_id):
        swept.append(_ending_of(conn, run_id))
    if swept:
        outcome, reason = swept[0]
        raise RunSwept(run_id, outcome, reason) from failure
    if failure is not None:
        raise failure


def _beat(path, run_id, interval_s, stop, swept, on_swept):
    """`heartbeat_while()`'s timer thread: beat until `stop`, or until swept.

    Opens its own connection to the store at `path`. A beat that finds the
    run ended appends the ending to `swept`, calls `on_swept` once, and
    returns: there is nothing left to keep alive.
    """
    try:
        own = store.open(path)
    except Exception as e:  # noqa: BLE001 - best effort; never the run's
        print(f"[holo2] heartbeat failed: {e}")
        return
    try:
        while not stop.wait(interval_s):
            try:
                if store.heartbeat(own, run_id):
                    continue
                swept.append(_ending_of(own, run_id))
            except Exception as e:  # noqa: BLE001 - same
                print(f"[holo2] heartbeat failed: {e}")
                continue
            # Swept: the run is over. Kill the turn, then stop beating.
            if on_swept is not None:
                try:
                    on_swept()
                except Exception as e:  # noqa: BLE001 - the raise follows
                    print(f"[holo2] stopping the swept turn failed: {e}")
            return
    finally:
        own.close()


def _ending_of(conn, run_id):
    """The `(outcome, outcomeReason)` the store recorded for ended run `run_id`.

    Read through the store's ended-runs view rather than SQL of this module's
    own. `(None, None)` if the run is not among them -- a beat can find the
    row gone only in a test that deleted it, but the raise must still name
    the run.
    """
    for run in store.read.ended_runs(conn):
        if run.id == run_id:
            return run.outcome, run.outcomeReason
    return None, None


def record_round(target, conn, run_id, rnd, role, reply, verify_cmd, ok, out,
                 started_at=None, criteria=(), root=None, route=None):
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
    checklist. `root` is the round's worktree: with it, a `met` witness that
    names a test must exist there (`criteria_findings()`), so a named test
    not found is one more such finding.

    `route` names what issued the round when it was not the role's agent
    route: a babysit pass over a pull request is stamped `github:LOGIN`,
    the reviewer whose threads the pass answered, so FINDINGS shows it
    beside the Codex rounds as what it was. None is `agent_route()`'s
    answer for `role`, as before.

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
        unwitnessed = criteria_findings(reply, criteria, root)
        if unwitnessed:
            verdict = "changes_requested"
            findings = findings + unwitnessed
    # `run_verify()` reports a pass/fail gate rather than a raw status — the
    # failing clause and its exit code live in the output it builds — so the
    # exit code stored here is that verdict, and `output` is the detail.
    results = ([{"command": verify_cmd, "exitCode": 0 if ok else 1,
                 "output": out}] if verify_cmd else [])
    store.record_review_round(conn, run_id, rnd, verdict,
                              route or agent_route(target, role),
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
