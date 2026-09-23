"""Supervisor-owned schema startup, serialized with merges."""
import json
import sqlite3
from time import time

import store
import store.read
from holophyte.gates import (
    MergeLockHeld,
    merge_lock,
    merge_lock_path,
    read_merge_lock,
    remove_dead_merge_lock,
)
from store.launch_backoff import event


def wait_for_migration(target, stop, out):
    """Retry lock contention at startup; a long merge must not kill the owner."""
    while not stop.is_set():
        try:
            migrate_store(target, out)
        except MergeLockHeld as exc:
            # Each acquisition already polls for the full lock wait. A live
            # holder is waited out; one whose run has ended is taken over by
            # the next attempt, under the sweep's own stale-lock rule.
            print(f"[holo2] supervisor waiting for merge lock before migration:"
                  f" {exc}; retrying", file=out, flush=True)
        else:
            return


def migration_version(path):
    """Read the stamp without writing; owners cannot use a newer schema."""
    if not path.exists():
        return 0
    conn = store.read.open_readonly(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    if version > store.SCHEMA_VERSION:
        raise store.SchemaNewer(path, version)
    return version


def _ended(path, run_id):
    """Why the sweep would call `run_id`'s lock stale, or None if it is live.

    Only `endedAt` is read: it predates every schema an owner migrates from,
    and `run_snapshot()` names columns an older store may not have yet."""
    if not path.exists():
        return "not in the store"
    conn = store.read.open_readonly(path)
    try:
        row = conn.execute("SELECT endedAt FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None  # no runs table yet: no run can be named
    finally:
        conn.close()
    if row is None:
        return "not in the store"
    return "ended" if row[0] is not None else None


# The holder a migration's merge lock names in place of a run: the owner has
# no run, and `read_merge_lock()` reads this word as "names no run", so the
# gate and the sweep treat it as they treat any unnamed lock.
MIGRATION_HOLDER = "migration"


def _stale_holder(target, path):
    """`(run_id, why)` for a lock this owner may take, or None to leave it.

    A lock naming a run is judged by the sweep's rule (`_ended()`); one the
    migration itself wrote names no run, so it is judged by its process
    alone -- `remove_dead_merge_lock()`'s flock probe. Any other unnamed
    lock is left alone, as the sweep leaves it."""
    try:
        first = path.read_text().split()[:1]
    except FileNotFoundError:
        return None
    if first == [MIGRATION_HOLDER]:
        return None, "owner exited"
    holder = read_merge_lock(path)
    if holder is None or holder[0] is None:
        return None
    why = _ended(target.store_path, holder[0])
    return None if why is None else (holder[0], why)


def take_stale_lock(target, out):
    """Remove a merge lock no live holder can still use; return what was taken.

    The rule is `merge_lock_lines()`'s: a lock naming a run that has ended,
    or that the store does not know, is stale; a lock naming a live run is
    left alone. `--sweep --act` cannot clear it for an older store, which
    only this owner may open, so the owner applies the rule itself. A lock
    an earlier migration left when its supervisor died mid-migration is
    taken too: nothing but a migrator writes it, and the sweep never clears
    an unnamed lock. Both go through `remove_dead_merge_lock()`, which
    refuses a lock whose creating process still holds its flock. The line
    is printed before the removal and the takeover rides in the migration
    event."""
    path = merge_lock_path(target)
    stale = _stale_holder(target, path)
    if stale is None:
        return None
    run_id, why = stale
    what = (f"left by a migration whose {why}" if run_id is None
            else f"run {run_id} {why}")
    print(f"[holo2] supervisor taking over stale merge lock before migration:"
          f" {what}", file=out, flush=True)
    outcome = remove_dead_merge_lock(path)
    if outcome != "removed":
        print(f"[holo2] stale merge lock ({what}) not taken: {outcome}",
              file=out, flush=True)
        return None
    if run_id is None:
        return {"holder": MIGRATION_HOLDER, "why": why}
    return {"run": run_id, "why": why}


def migrate_store(target, out=None):
    """Run once at supervisor startup, including startup after exec."""
    # A current store must reach the sweep even if a stale merge lock exists.
    if migration_version(target.store_path) == store.SCHEMA_VERSION:
        return
    target.store_path.parent.mkdir(parents=True, exist_ok=True)
    taken = take_stale_lock(target, out)
    with merge_lock(target, MIGRATION_HOLDER, operation="migration"):
        # Another owner may have stamped the store while we waited.
        version = migration_version(target.store_path)
        if version == store.SCHEMA_VERSION:
            return
        def record(conn, from_version):
            # Inside init()'s transaction: the stamp never commits without it.
            # Only an already registered project carries the event: creating
            # its row here would refuse the operator's `project add` later.
            # init()'s `migrate` intervention records a fresh store's stamp.
            row = conn.execute(
                "SELECT id FROM projects WHERE repoPath = ? ORDER BY id LIMIT 1",
                (str(target.path),)).fetchone()
            if row is None:
                return
            summary = {"from": from_version, "to": store.SCHEMA_VERSION}
            if taken:
                summary["staleLock"] = taken
            event(conn, row[0], "migration", json.dumps(summary),
                  int(time() * 1000))

        store.open(target.store_path, migrate="owner", on_migrate=record).close()
