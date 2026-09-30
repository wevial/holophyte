import sys
from pathlib import Path
from time import time

import review_runner
import store.read
from holophyte.cli.report import REPORT_GAP, failure_lines, host_label
from holophyte.loop.gates import (
    merge_lock_path,
    read_merge_lock,
    remove_dead_merge_lock,
)


def merge_lock_lines(target, conn, act=False):
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
    # Judged and unlinked under the gate's own arbiter: never races an acquisition.
    outcome = remove_dead_merge_lock(path)
    if outcome == "removed":
        return [f"removed stale merge lock: run {run_id} {why}"]
    if outcome == "in_use":
        return [f"merge lock names run {run_id} ({why}) but its process is"
                " alive and holds it; left alone"]
    return [f"stale merge lock: run {run_id} {why}; already cleared"]


SWEEP_HEADERS = ("ticket", "run", "phase", "condition", "evidence", "host")


SWEEP_HINT = ("[holo2] tripped runs are failed by"
              " `factory.py {project} --sweep --act`;"
              " a bare --sweep re-checks first")


def _runs(n):
    return "1 run" if n == 1 else f"{n} runs"


def sweep_lines(result, target=None):
    return (restart_lines(result) + list(result.locks)
            + run_lines(result, target))


def restart_lines(result):
    return [f"loop did not return after re-exec from {sha}:"
            f" no claim, heartbeat or exit note in the {age / 60000:.1f} min"
            " since the exec"
            for sha, age in result.restarts]


def run_lines(result, target=None):
    if not result.swept:
        return ["no runs in flight, nothing to sweep"]
    if not result.trips:
        # A first-strike sighting must not read as health.
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
    from holophyte.host.supervisor import sweep
    out = out or sys.stdout
    if conn is None and not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return
    # Containers first, so the run summary stays the last line.
    print("\n".join(review_container_lines(act)), file=out)
    owned = conn is None
    conn = conn if conn is not None else store.open(target.store_path, migrate=act)
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
    from holophyte.config.project import worktree_path

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
