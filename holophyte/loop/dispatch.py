"""The run dispatch wrapper and its crash containment."""
import traceback
from pathlib import Path
from time import time

import store
from holophyte.agents.review_workspace import cleanup_review_refs
from holophyte.board.projection import (
    body_problems,
    close_out_failure,
    mirror_status,
    mirror_task,
    note_problems,
    on_pull_request,
    release_lease_label,
    release_run,
)
from holophyte.host.supervisor import linear_budget_low, sweep
from holophyte.host.sweep_report import SWEEP_HINT, sweep_lines
from holophyte.loop.gates import MergeParked, RunFailure, outcome_class_of
from holophyte.redact import safe_print as print
from holophyte.review.findings import refresh_findings


def _startup_sweep(target, conn):
    """Read-only: one silence is a first strike, never evidence to fail on."""
    from holophyte.admission import lines
    for line in lines(conn):
        print(line)
    seen = sweep(target, conn, int(time() * 1000))
    if seen.trips or seen.watched or seen.restarts:
        print("\n".join(sweep_lines(seen, target)))
        if seen.trips:
            print(SWEEP_HINT.format(project=target.path))
    return seen


def _mirror_queue(target, conn, project, provider):
    """None, not [], when skipped: an unasked board said nothing about the queue."""
    from holophyte.admission import held_line
    if held_line(conn, project) or linear_budget_low():
        return None
    store_mode = getattr(provider, "store_mode", False)
    mirrored = []
    try:
        for task in provider.listing() if store_mode else provider.ready_issues():
            problems = body_problems(
                task, target.path,
                on_pull_request=on_pull_request(conn, project, task))
            blocked_by = task["blocked_by"] if store_mode else None
            ticket_id = mirror_task(conn, project, task,
                                    specced=not problems,
                                    depends_on=blocked_by)
            if problems and store_mode:
                note_problems(conn, ticket_id, "validation", task["body"],
                              problems)
            if blocked_by:
                _wait_on_blockers(conn, ticket_id)
            else:
                mirrored.append(task)
    except Exception as e:
        print(f"[holo2] queue mirror skipped: the board's ready issues could"
              f" not be mirrored ({e})")
        return None
    return mirrored


def _wait_on_blockers(conn, ticket_id):
    with store.transaction(conn):
        if store.read.ticket_by_id(conn, ticket_id).status == "ready":
            store.tickets.walk_ticket(conn, ticket_id, "blocked_on_deps")


ROOT = Path(__file__).resolve().parents[2]


def _factory_frame(e):
    try:
        for frame in reversed(traceback.extract_tb(e.__traceback__)):
            path = Path(frame.filename)
            if not path.is_absolute():
                continue
            try:
                rel = path.resolve().relative_to(ROOT)
            except ValueError:
                continue
            if "site-packages" in rel.parts:
                continue
            return f"{rel.as_posix()}:{frame.name}:{frame.lineno}"
    except Exception:
        pass
    return None


def crash_reason(e):
    """One line: it lands verbatim in an escalation comment's markdown bullet."""
    reason = " ".join(f"{type(e).__name__}: {e}".split())
    frame = _factory_frame(e)
    return f"{reason} (at {frame})" if frame else reason


def _record_crash(conn, run_id, e, reason):
    if conn is None:
        return
    # The store may be what crashed the run; its failure must not replace the reason.
    try:
        store.record_event(
            conn, run_id, "crash", reason, level="detail",
            payload="".join(traceback.format_exception(type(e), e,
                                                       e.__traceback__)))
    except Exception as err:
        print(f"[holo2] crash event not recorded: {err}")


class _Parked:
    def __bool__(self):
        return False


PARKED = _Parked()


class _Swept:
    def __bool__(self):
        return False

    def __repr__(self):
        return "SWEPT"


SWEPT = _Swept()


def _dispatch(target, conn, run_id, provider, task, ticket_id, refresh=True):
    from holophyte.loop.pipeline import run_task

    merged = False
    reason = None
    outcome_class = "work"
    failure_kind = "unclassified"
    try:
        merged = run_task(target, task, conn, run_id, provider)
    except MergeParked as e:
        # `store.park()` already gave the lease back: nothing to release or close out.
        merged = PARKED
        print(f"[holo2] run parked: {e}")
    except RunFailure as e:
        reason = e.reason
        failure_kind = e.failure_kind
        outcome_class = outcome_class_of(e)
        print(f"[holo2] run failed: {reason}")
    except Exception as e:
        reason = crash_reason(e)
        print(f"[holo2] run crashed: {reason}")
        _record_crash(conn, run_id, e, reason)
    finally:
        cleanup_review_refs(target.path, run_id)
        if merged is PARKED:
            release_lease_label(target, conn, ticket_id, provider, run_id)
        elif merged is SWEPT:
            pass
        elif merged:
            release_run(conn, run_id, True,
                        merge_sha=merged if isinstance(merged, str) else None)
            mirror_status(conn, ticket_id, "merged", provider)
            release_lease_label(target, conn, ticket_id, provider, run_id)
            if refresh:
                refresh_findings(target, conn)
        else:
            # Its own failure must not replace the one in flight; the lease stays.
            try:
                close_out_failure(target, conn, run_id, ticket_id,
                                  reason,
                                  provider=provider,
                                  outcome_class=outcome_class,
                                  refresh=refresh, failure_kind=failure_kind)
            except Exception as close_err:
                print(f"[holo2] close-out failed: {close_err}")
    return merged
