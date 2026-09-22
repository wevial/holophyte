"""The run dispatch wrapper and its crash containment.

`_dispatch()` runs one claim end to end -- `run_task()` under its failure
accounting and close-out -- and answers whether it merged, `PARKED` for a
run stopped at the merge gate for a human, or `SWEPT` for one the
supervisor ended mid-turn. `crash_reason()`, `_factory_frame()` and
`_record_crash()` turn an exception that escaped the run into the one-line
close-out reason and the traceback event behind it; `_startup_sweep()` and
`_mirror_queue()` are the pass's opening steps, shared by the serial loop
and the scheduler.

Moved verbatim out of `holophyte/loop.py` (KO-412); the one back-reference,
`run_task()`, is imported inside `_dispatch()`.
"""
import traceback
from pathlib import Path
from time import time

import store
from holophyte.agents import cleanup_review_refs
from holophyte.board import (
    body_problem,
    close_out_failure,
    mirror_status,
    mirror_task,
    release_lease_label,
    release_run,
)
from holophyte.findings import refresh_findings
from holophyte.gates import MergeParked, RunFailure, outcome_class_of
from holophyte.redact import safe_print as print
from holophyte.supervisor import linear_budget_low, sweep
from holophyte.sweep_report import SWEEP_HINT, sweep_lines


def _startup_sweep(target, conn):
    """Startup self-sweep, read-only: it records what it saw — a first
    strike on anything silent — so the *next* invocation or a
    `--sweep --act` can act on the second sighting. Nothing is failed
    from here; one sample is not evidence (STALE_STRIKES). One sweep,
    printed once, per invocation: the refused-claim handler below
    points back at these lines rather than re-sweeping (which would
    count one silence twice) or reprinting (which would look like it
    had).
    """
    from holophyte.admission import lines
    for line in lines(conn):
        print(line)
    seen = sweep(target, conn, int(time() * 1000))
    if seen.trips or seen.watched or seen.restarts:
        print("\n".join(sweep_lines(seen, target)))
        if seen.trips:
            print(SWEEP_HINT.format(target=target.path))
    return seen


def _mirror_queue(target, conn, project, provider):
    """Mirror every issue the board lists as ready, before the claim picks
    one, so the Board shows the queue and not only the claimed ticket.

    The Board reads the store's mirror and nothing more, and until now the
    mirror held only what the loop had claimed: the operator filed four
    tickets and saw none of them (KO-334). The listing is the one the claim
    chooses from, so a ticket the loop would not claim -- Backlog, closed,
    blocked in Linear -- is not shown either. Each candidate takes the same
    body-driven route the claim takes in `_admit_ticket()`: a body the
    template validator rejects is mirrored with `specced=False` and lands
    in `needs_spec`, a valid one lands where its lists put it. Statuses
    that are somebody's decision -- `in_flight`, `blocked_on_operator`,
    `blocked_on_deps`, the terminal ones -- are left alone by
    `store.tickets.mirror_ticket()` itself, and dependencies are left as the store
    has them. A board that cannot be asked, or a listing the mirror
    chokes on, skips the whole step in one printed line and the claim
    proceeds: this fills the Board, it does not gate the work. Nothing is
    written to Linear. Returns the listing it mirrored -- the scheduler
    counts its claimable tickets from it (KO-343) -- and None when the step
    was skipped: a board that could not be asked has said nothing about the
    queue, and an empty list would say it is empty. A Linear complexity
    budget under its tenth is skipped the same way (KO-434): the relisting
    waits for the reset `linear_budget_low()` names once rather than
    spending the points to be refused.
    """
    from holophyte.admission import held_line
    if held_line(conn, project) or linear_budget_low():
        return None
    mirrored = []
    try:
        for task in provider.ready_issues():
            specced = body_problem(task, target.path) is None
            mirror_task(conn, project, task, specced=specced)
            mirrored.append(task)
    except Exception as e:  # any transport or mirror failure: not a gate
        print(f"[holo2] queue mirror skipped: the board's ready issues could"
              f" not be mirrored ({e})")
        return None
    return mirrored


# The repository the factory runs from, for telling its own frames in a
# crash's traceback from the standard library's and a dependency's.
ROOT = Path(__file__).resolve().parent.parent


def _factory_frame(e):
    """`path:function:line` of the innermost traceback frame of `e` whose
    file lives under `ROOT` — the deepest place in the factory's own code the
    exception escaped from — or None when no frame does. Never raises: a
    reason is worth more than a frame, so any trouble reading the traceback
    answers None."""
    try:
        for frame in reversed(traceback.extract_tb(e.__traceback__)):
            path = Path(frame.filename)
            if not path.is_absolute():
                continue  # `<string>`, `<frozen ...>`: nowhere to point at
            try:
                rel = path.resolve().relative_to(ROOT)
            except ValueError:
                continue
            if "site-packages" in rel.parts:
                continue  # a dependency installed inside the repository
            return f"{rel.as_posix()}:{frame.name}:{frame.lineno}"
    except Exception:  # noqa: BLE001 - the frame is a bonus, never a failure
        pass
    return None


def crash_reason(e):
    """The one-line close-out reason for an exception that escaped
    `run_task()`: `TYPE: message`, then `(at path:function:line)` naming the
    innermost frame in the factory's own code when the traceback has one.

    One line because sh()'s message carries the failed command's whole
    output, and the reason lands verbatim in an escalation comment's
    markdown bullet. The frame is what run 103 (KO-273) lacked: `database is
    locked` with nothing to say which write raised it."""
    reason = " ".join(f"{type(e).__name__}: {e}".split())
    frame = _factory_frame(e)
    return f"{reason} (at {frame})" if frame else reason


def _record_crash(conn, run_id, e, reason):
    """Keep the whole traceback as a `detail` event of kind `crash` before
    the close-out moves the run to `failed`. Best effort: the store may be
    the very thing that crashed the run, and its failure must not replace
    the reason in flight."""
    if conn is None:
        return
    try:
        store.record_event(
            conn, run_id, "crash", reason, level="detail",
            payload="".join(traceback.format_exception(type(e), e,
                                                       e.__traceback__)))
    except Exception as err:  # noqa: BLE001 - see the docstring
        print(f"[holo2] crash event not recorded: {err}")


class _Parked:
    """`_dispatch()`'s answer for a run parked awaiting merge approval:
    falsy, because nothing merged, and its own object, because the loop
    goes on rather than stopping on a failure."""

    def __bool__(self):
        return False


PARKED = _Parked()


class _Swept:
    """`_dispatch()`'s answer for a run the supervisor ended mid-turn: the
    heartbeat noticed, the turn was killed, and the sweep's own close-out is
    the last word on the run. Falsy like a failure, so no caller mistakes it
    for a merge, and its own object so `main()` can tell it from one."""

    def __bool__(self):
        return False

    def __repr__(self):
        return "SWEPT"


SWEPT = _Swept()


def _dispatch(target, conn, run_id, provider, task, ticket_id, refresh=True):
    """One run of `task` under `run_id`, with its failure accounting and
    close-out. Returns whether the run merged, `PARKED` for a run stopped
    at the gate by `[merge] approve = "human"`, or `SWEPT` for a run the
    supervisor ended mid-turn, whose close-out the sweep already did.

    `run_task()` answers with the merge commit's sha when it merged, and
    that sha is what the release stamps on the run; a bare `True` (the
    supervisor ended the run as merged, or a test's stand-in) merges the
    run without one. `refresh=False` leaves the run's FINDINGS.md
    regeneration -- a merged run's and a failed run's alike -- to the
    caller: a worker does it under the merge lock
    (`_render_findings_locked()`), where the file is not written beside a
    sibling's merge."""
    from holophyte.loop import run_task

    merged = False
    reason = None
    outcome_class = "work"
    try:
        merged = run_task(target, task, conn, run_id, provider)
    except MergeParked as e:
        # Not a failure and not an ending: `store.park()` has already moved
        # the run to `awaiting_merge_approval` and given the lease back, so
        # there is nothing to release and nothing to close out below.
        merged = PARKED
        print(f"[holo2] run parked: {e}")
    except RunFailure as e:
        reason = e.reason
        outcome_class = outcome_class_of(e)
        print(f"[holo2] run failed: {reason}")
    except Exception as e:  # noqa: BLE001 - crash containment
        # Anything that escapes run_task is this run's failure. The
        # error text becomes the close-out reason — one clean line
        # naming the factory frame it escaped from, instead of a
        # traceback with the reason lost to release_run()'s generic
        # default (KO-146 incident, run 9). The traceback itself goes
        # to the run's events, where the close-out below cannot lose it.
        reason = crash_reason(e)
        print(f"[holo2] run crashed: {reason}")
        _record_crash(conn, run_id, e, reason)
    finally:
        cleanup_review_refs(target.path, run_id)
        if merged is PARKED:
            # Parked, alive, lease released: the run's own outcome is still
            # open, so there is no entry to render and no failure to count.
            # The board lease goes with the store lease `store.park()` gave
            # back: a parked ticket is a human's, not this writer's.
            release_lease_label(target, conn, ticket_id, provider, run_id)
        elif merged is SWEPT:
            # The sweep already ended, released, unlabelled and rendered it.
            pass
        elif merged:
            release_run(conn, run_id, True,
                        merge_sha=merged if isinstance(merged, str) else None)
            # `in_flight -> merged`, projected as Done. A run that did
            # not merge leaves the ticket in flight on purpose: the
            # branch is preserved for a human and the board should go
            # on saying the work is open, so there is nothing to push.
            mirror_status(conn, ticket_id, "merged", provider)
            release_lease_label(target, conn, ticket_id, provider, run_id)
            # Close-out, and the first moment the run's own outcome is
            # a row: the window is regenerated here rather than inside
            # `run_task()` so the entry that ends the run is in it.
            if refresh:
                refresh_findings(target, conn)
        else:
            # The failure close-out: release, escalate if this failure
            # was one too many, regenerate the window. Shared with the
            # supervisor sweep, which fails runs this loop is no
            # longer around to fail itself. Its own failure (a locked
            # store, say) must not replace what was in flight — a
            # KeyboardInterrupt included — with a traceback of its
            # own; the lease stays for release() or the sweep.
            try:
                close_out_failure(target, conn, run_id, ticket_id,
                                  reason,
                                  provider=provider,
                                  outcome_class=outcome_class,
                                  refresh=refresh)
            except Exception as close_err:  # noqa: BLE001
                print(f"[holo2] close-out failed: {close_err}")
    return merged
