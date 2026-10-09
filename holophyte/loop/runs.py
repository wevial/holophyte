import threading
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from time import time

import review_runner
import store
import store.read
from holophyte.agents.roles import agent_route
from holophyte.redact import safe_print as print
from holophyte.review.reply_parsing import (
    criteria_findings,
    parse_findings,
    raw_finding,
    round_verdict,
    sanitize_findings,
)

MAX_ROUNDS = 2


def review_round_cap(changed_lines, cfg):
    extra = (changed_lines // cfg.review_rounds_per_lines
             if cfg.review_rounds_per_lines else 0)
    return min(cfg.review_rounds_max, cfg.review_rounds + extra)


def open_store(target, path=None):
    path = Path(path or target.store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return store.open(str(path))


def set_phase(conn, run_id, phase, note=None):
    """The single writer of `runs.phase`: phase, heartbeat and event move together."""
    if conn is None:
        return
    from holophyte.loop.stop import stop_if_requested
    stop_if_requested(conn, run_id, phase)
    store.set_phase(conn, run_id, phase, note)


class RunSwept(Exception):
    def __init__(self, run_id, outcome, reason):
        super().__init__(f"run {run_id} was swept ({outcome}: {reason})")
        self.run_id, self.outcome, self.reason = run_id, outcome, reason


@contextmanager
def heartbeat_while(conn, run_id, interval_s, on_swept=None):
    if conn is None or run_id is None:
        yield
        return
    (path,) = [row[2] for row in conn.execute("PRAGMA database_list")
               if row[1] == "main"]
    stop = threading.Event()
    swept = []  # the ended row's (outcome, reason), set once by the beat
    thread = threading.Thread(
        target=_beat, args=(path, run_id, interval_s, stop, swept, on_swept,
                           partial(_fallback_heartbeat, conn, run_id, swept,
                                   stop)),
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
    # One more beat after the join: a run ended since the timer's last beat.
    if not swept and not store.heartbeat(conn, run_id):
        swept.append(_ending_of(conn, run_id))
    from holophyte.loop.stop import Aborted, abort_requested, end_aborted
    if swept[:1] == [ABORT] or not swept and abort_requested(conn, run_id):
        end_aborted(conn, run_id)
        swept.clear()
    if isinstance(failure, Aborted):
        raise failure
    if swept:
        outcome, reason = swept[0]
        if outcome == "paused":
            raise store.RunEnded(run_id, outcome, reason) from failure
        raise RunSwept(run_id, outcome, reason) from failure
    if failure is not None:
        raise failure


def _fallback_heartbeat(conn, run_id, swept, stop):
    while not stop.is_set():
        if conn._lock.acquire(blocking=False):
            try:
                return _heartbeat(conn, run_id, swept)
            finally:
                conn._lock.release()
        stop.wait(0.05)
    # Cancellation is not a swept run.
    return True


ABORT = ("abort", None)


def _heartbeat(conn, run_id, swept):
    from holophyte.loop.stop import abort_requested
    with store.transaction(conn):
        if store.heartbeat(conn, run_id):
            if not abort_requested(conn, run_id):
                return True
            swept.append(ABORT)
            return False
        swept.append(_ending_of(conn, run_id))
        return False


def _beat(path, run_id, interval_s, stop, swept, on_swept, heartbeat):
    own = None
    failed = False

    def current_heartbeat():
        nonlocal own, failed
        if own is None:
            try:
                own = store.open(path)
            except BaseException as exc:  # noqa: BLE001 - newer schema is SystemExit
                if not failed:
                    print(f"[holo2] heartbeat failed: {exc};"
                          " beating through the open connection", flush=True)
                failed = True
                return heartbeat
        return partial(_heartbeat, own, run_id, swept)

    try:
        while not stop.wait(interval_s):
            try:
                alive = current_heartbeat()()
                if alive:
                    # The open failure persists until the timer's own
                    # connection can beat.
                    if failed and own is not None:
                        print("[holo2] heartbeat recovered", flush=True)
                        failed = False
                    continue
            except Exception as e:  # noqa: BLE001 - same
                if not failed:
                    print(f"[holo2] heartbeat failed: {e}", flush=True)
                failed = True
                continue
            _notify_swept(on_swept)
            return
    finally:
        if own is not None:
            own.close()


def _notify_swept(on_swept):
    if on_swept is not None:
        try:
            on_swept()
        except Exception as exc:  # noqa: BLE001 - the raise follows
            print(f"[holo2] stopping the swept turn failed: {exc}")


def _ending_of(conn, run_id):
    for run in store.read.ended_runs(conn):
        if run.id == run_id:
            return run.outcome, run.outcomeReason
    return None, None


def record_round(target, conn, run_id, rnd, role, reply, verify_cmd, ok, out,
                 started_at=None, criteria=(), root=None, route=None,
                 prior_reply="", structured_findings=None, approved_range=None,
                 scope=(), adversary=()):
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
    if structured_findings is not None:
        findings = structured_findings
    if role == "review" and verdict != "error":
        unwitnessed = criteria_findings(reply, criteria, root,
                                        approved_range=approved_range,
                                        scope=scope) + list(adversary)
        if unwitnessed:
            verdict = "changes_requested"
            findings = findings + unwitnessed
    if prior_reply:
        prior_reply = sanitize_findings(prior_reply, len(prior_reply))
        findings = [dict(raw_finding(prior_reply), message=prior_reply,
                         evidence_only=True)] + findings
    # The exit code stored is `run_verify()`'s verdict; `output` is the detail.
    results = getattr(out, "results", None)
    if results is None:
        results = ([{"source": "ticket", "command": verify_cmd,
                     "exitCode": 0 if ok else 1,
                     "output": out}] if verify_cmd else [])
    store.record_review_round(conn, run_id, rnd, verdict,
                              route or agent_route(target, role),
                              findings=findings, verification_results=results,
                              started_at=started_at,
                              ended_at=int(time() * 1000))


def warn_on_run(conn, run_id, summary):
    print(f"[holo2] {summary}")
    if conn is None or run_id is None:
        return
    store.record_event(conn, run_id, "warning", summary)
