"""holophyte.sweep_report: the sweep's report lines (KO-396).

Moved verbatim out of `holophyte/supervisor.py`: the merge lock's line
(`merge_lock_lines()`), the `SWEEP_HEADERS` table columns and the
`SWEEP_HINT` a printed table is followed by, `sweep_lines()` and its halves
`restart_lines()` and `run_lines()` with the `_runs()` counting helper,
`sweep_report()` as `--sweep`'s whole body, and the `review containers`
section (`review_container_lines()`). `sweep_report()` reaches `sweep()`
back in `holophyte.supervisor` through a deferred import, so the import
runs one way: the sweep module names this module's renderers for the lines
it prints -- `merge_lock_lines()` inside `sweep()`'s transaction and
`sweep_lines()` on a loud `supervise_pass()`.
"""
import sys
from pathlib import Path
from time import time

import review_runner
import store.read
from holophyte.gates import merge_lock_path, read_merge_lock, remove_dead_merge_lock
from holophyte.report import REPORT_GAP, failure_lines, host_label


def merge_lock_lines(target, conn, act=False):
    """The merge lock's line, if there is a lock: held, stale, or removed.

    The gate takes `gates.merge_lock()` for the span of a merge and gives it
    back on every way out, so a lock still on disk names either a gate in
    progress or a run that died holding it. The run it names decides which:
    live (no `endedAt`) and the lock is reported as held; ended, or unknown
    to the store, and it is stale -- a bare sweep says so, an acting sweep
    removes it, and the line names the run either way. A lock that names no
    run (a storeless `run_task()` wrote it, or it is half-written) cannot be
    judged and is left alone, said so.
    """
    path = merge_lock_path(target)
    holder = read_merge_lock(path)
    if holder is None:
        return []
    run_id, taken_at = holder
    if run_id is None:
        return [f"merge lock {path} names no run; left alone"]
    snapshot = store.read.run_snapshot(conn, run_id)
    if snapshot is not None and snapshot.endedAt is None:
        age = (f" for {(time() - taken_at) / 60:.1f} min"
               if taken_at is not None else "")
        return [f"merge lock held by run {run_id} ({snapshot.phase}){age}"]
    why = ("ended" if snapshot is not None else "not in the store")
    if not act:
        return [f"stale merge lock: run {run_id} {why};"
                " --sweep --act removes it"]
    # Removal must not race a gate's acquisition or another sweep: the
    # helper judges and unlinks under the same arbiter the gate creates
    # under, and takes the lock's flock first (refused while the creating
    # process lives). A live lock is left where it is, and the line says so.
    outcome = remove_dead_merge_lock(path)
    if outcome == "removed":
        return [f"removed stale merge lock: run {run_id} {why}"]
    if outcome == "in_use":
        return [f"merge lock names run {run_id} ({why}) but its process is"
                " alive and holds it; left alone"]
    return [f"stale merge lock: run {run_id} {why}; already cleared"]


SWEEP_HEADERS = ("ticket", "run", "phase", "condition", "evidence", "host")


# Printed under a table with trips in it wherever the reader is an operator
# who did not ask for a sweep (startup, a refused claim): the table says what
# is wrong, this says what to type. `{project}` is filled at the print site so
# the line is copy-pasteable for a non-default project.
SWEEP_HINT = ("[holo2] tripped runs are failed by"
              " `factory.py {project} --sweep --act`;"
              " a bare --sweep re-checks first")


def _runs(n):
    """`n` runs, counted in English -- the summary line reads as a sentence."""
    return "1 run" if n == 1 else f"{n} runs"


def sweep_lines(result, target=None):
    """The sweep as lines: a header, one line per trip, a summary.

    A clean sweep prints what it checked rather than nothing. Empty output is
    ambiguous -- it reads the same as a crashed supervisor, a mistyped target
    or a store with no runs in it -- so the quiet case is an assertion an
    operator can act on, and the three quiet cases say which one they are.

    An acting sweep adds one outcome line per trip and a summary that counts
    the failed apart from the declined. Both come from `Outcome`, which is
    what `act_on_trip()` actually did, and never from the `acted` flag the
    sweep was called with: a re-check that stood down because the run had
    finished is reported as exactly that, naming the status it found, and
    the words "failed and leases released" are printed only for a run whose
    failure was written. A read-only sweep has no outcomes and prints as it
    always has.

    A restart the loop did not come back from is printed first, one line per
    restart naming the sha and how long ago the exec was: it is not about a
    run, so it sits above the run table, and it is printed above the quiet
    lines too, because "no runs in flight" is exactly what a loop that died
    in its exec leaves behind.
    """
    return (restart_lines(result) + list(result.locks)
            + run_lines(result, target))


def restart_lines(result):
    """One line per self-merge re-exec the loop did not come back from."""
    return [f"loop did not return after re-exec from {sha}:"
            f" no claim, heartbeat or exit note in the {age / 60000:.1f} min"
            " since the exec"
            for sha, age in result.restarts]


def run_lines(result, target=None):
    """`sweep_lines()` less the restart lines: the per-run report.

    `target` supplies the `[report] host_label` the host column shows in
    place of the hostname; without one the column is the hostname itself.
    """
    if not result.swept:
        return ["no runs in flight, nothing to sweep"]
    if not result.trips:
        # A first-strike sighting must not read as health: "none tripped"
        # plus the watched lines is the honest quiet case.
        if result.watched:
            return [f"{_runs(result.swept)} swept, none tripped",
                    *result.watched]
        return [f"{_runs(result.swept)} swept, all healthy"]
    table = [SWEEP_HEADERS]
    table += [(trip.ticket, f"run {trip.run_id}", trip.phase, trip.condition,
               trip.evidence, host_label(target, trip.host))
              for trip in result.trips]
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [
        REPORT_GAP.join(cell.ljust(width)
                        for cell, width in zip(row, widths)).rstrip()
        for row in table
    ]
    for outcome in result.outcomes:
        run_id = outcome.trip.run_id
        if outcome.acted:
            lines.append(f"acted: failed run {run_id}, leases released")
        else:
            status = ("gone" if outcome.phase is None
                      else f"now {outcome.phase}")
            lines.append(f"declined: run {run_id} is {status}; no action")
    lines += list(result.watched)
    failed = sum(1 for outcome in result.outcomes if outcome.acted)
    declined = len(result.outcomes) - failed
    summary = f"{len(result.trips)} tripped of {_runs(result.swept)} swept"
    if failed:
        summary += f", {failed} failed and leases released"
    if declined:
        summary += f", {declined} declined, no action"
    return lines + [summary]


def sweep_report(target, conn=None, now=None, out=None, act=False, provider=None):
    """Print the target store's tripped runs, failing them when `act`.

    `--sweep`'s whole body, and a sibling of `report()` in what it refuses to
    do: no ticket is claimed and no worktree is cut, so it is safe to run
    against the store of a loop that is still working -- the case it exists
    for. Unlike `report()` it does write, to exactly one table: the strike
    tally `sweep()` keeps, without which "two consecutive sweeps" could not
    span two invocations.

    `act` is what `--act` adds, and it adds it to nothing else: a pass that
    trips no run writes exactly what a read-only pass writes, so acting costs
    nothing on the sweeps that find everything healthy. A pass that does trip
    something fails those runs, and only then is a provider needed -- and only
    if a ticket has reached its escalation threshold.

    The table is printed after the acting rather than before it, so it is a
    record of what happened rather than a promise: a best-effort push that
    warns on its way past appears above the summary claiming the runs were
    failed, not below it.

    A target with no store has no runs to sweep and is reported rather than
    created, the way `--report` answers the same mistake.

    The `review containers` section comes first, so the run summary stays
    the last line: it asks Docker rather than the store, and a reviewer
    leaked by a loop that died is the one thing here the store cannot see.
    """
    from holophyte.supervisor import sweep
    out = out or sys.stdout
    if conn is None and not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return
    print("\n".join(review_container_lines(act)), file=out)
    owned = conn is None
    conn = conn if conn is not None else store.open(target.store_path)
    try:
        from holophyte.admission import lines
        for line in lines(conn):
            print(line, file=out)
        for line in debris_lines(target, conn):
            print(line, file=out)
        if now is None:
            now = int(time() * 1000)
        result = sweep(target, conn, now, act, provider)
        for line in failure_lines(conn):
            print(line, file=out)
        print("\n".join(sweep_lines(result, target)),
              file=out)
    finally:
        if owned:
            conn.close()


def debris_lines(target, conn):
    """Final tickets' factory checkout paths, reported only, even with --act."""
    from holophyte.project import worktree_path

    rows = conn.execute(
        "SELECT DISTINCT t.linearIdentifier, t.status, r.branch, p.repoPath"
        " FROM tickets t"
        " JOIN runs r ON r.ticketId = t.id JOIN projects p ON p.id = t.projectId"
        " WHERE t.status IN ('merged', 'abandoned') AND r.branch IS NOT NULL"
        " ORDER BY t.linearIdentifier, r.branch")
    lines = []
    for identifier, status, branch, repo_path in rows:
        if Path(repo_path).resolve() != target.path.resolve():
            continue
        path = worktree_path(target, branch)
        if (path.is_dir() and not path.is_symlink()
                and path.resolve().is_relative_to(target.worktrees.resolve())):
            lines.append(f"debris: {identifier} ({status}): {path}")
    return lines


def review_container_lines(act=False):
    """The `review containers` section: strays listed, and removed when `act`.

    A review container is removed by the loop that started it, on exit or on
    a stop signal; one still running after its scratch directory is gone
    belongs to a loop that died some other way (SIGKILL, a host reset) and
    holds two CPUs, 2 GB and a Codex session until something removes it. A
    container whose scratch directory still exists is a live review and is
    never touched. Without a `docker` to ask, the section says the check was
    skipped rather than claiming a clean host.
    """
    try:
        strays = review_runner.stray_containers()
    except review_runner.ReviewBoundaryError as e:
        return [f"review containers: skipped ({e})"]
    if not strays:
        return ["review containers: none stray"]
    lines = ["review containers:"]
    for name in strays:
        if not act:
            lines.append(f"  stray {name}")
            continue
        try:
            review_runner._remove_container(name)
        except review_runner.ReviewBoundaryError as e:
            lines.append(f"  stray {name}: {e}")
        else:
            lines.append(f"  removed stray {name}")
    return lines
