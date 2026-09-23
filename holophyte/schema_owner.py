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
    """Read the stamp without writing; `store.open()` judges a newer one.

    A newer stamp has nothing to migrate, and whether this build may still
    read it is its `readableFrom` floor's question, which only `open()`
    answers."""
    if not path.exists():
        return 0
    conn = store.read.open_readonly(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
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
# gate waits on it as on any unnamed lock; the sweep and the owner judge it
# by its process (`names_migration()`).
MIGRATION_HOLDER = "migration"

# The runEvents kind a dead migrator's lock is taken under once the store is
# current: no migration runs then, so the takeover has no `migration` event
# to ride in.
LOCK_TAKEN_EVENT = "migration_lock_taken"


def names_migration(path):
    """Whether the lock at `path` is one a migration wrote."""
    try:
        return path.read_text().split()[:1] == [MIGRATION_HOLDER]
    except FileNotFoundError:
        return False


def _project_id(conn, target):
    """The registered project a run-less event of `target` is recorded under."""
    row = conn.execute(
        "SELECT id FROM projects WHERE repoPath = ? ORDER BY id LIMIT 1",
        (str(target.path),)).fetchone()
    return None if row is None else row[0]


class _Kept(Exception):
    """Unwinds a takeover's transaction when the lock was not removed."""


def reclaim_migration_lock(target, conn, by):
    """Remove the lock a dead migrator left on a current store, recorded.

    Return `remove_dead_merge_lock()`'s outcome, or None when the lock is not
    a migration's. The `LOCK_TAKEN_EVENT` is written before the removal and
    in the same transaction, so a lock its migrator still holds (`in_use`)
    or one already `gone` rolls the event back and nothing claims a
    takeover that did not happen. A store with no registered project has
    no row to carry the event, as for the migration event itself."""
    path = merge_lock_path(target)
    if not names_migration(path):
        return None
    note = {"holder": MIGRATION_HOLDER, "why": "owner exited", "by": by}
    try:
        with store.transaction(conn):
            project = _project_id(conn, target)
            if project is not None:
                event(conn, project, LOCK_TAKEN_EVENT, json.dumps(note),
                      int(time() * 1000))
            outcome = remove_dead_merge_lock(path)
            if outcome != "removed":
                raise _Kept(outcome)
    except _Kept as kept:
        return kept.args[0]
    return "removed"


def _reclaim_on_current(target, out):
    """A migrator killed after its stamp committed left the lock behind; the
    store is current, so no migration will take it, and this one does."""
    if not names_migration(merge_lock_path(target)):
        return
    what = "left by a migration whose owner exited"
    print(f"[holo2] supervisor taking over stale merge lock: {what}",
          file=out, flush=True)
    conn = store.open(target.store_path, migrate="owner")
    try:
        outcome = reclaim_migration_lock(target, conn, "supervisor")
    finally:
        conn.close()
    if outcome not in ("removed", None):
        print(f"[holo2] stale merge lock ({what}) not taken: {outcome}",
              file=out, flush=True)


def _stale_holder(target, path):
    """`(run_id, why)` for a lock this owner may take, or None to leave it.

    A lock naming a run is judged by the sweep's rule (`_ended()`); one the
    migration itself wrote names no run, so it is judged by its process
    alone -- `remove_dead_merge_lock()`'s flock probe. Any other unnamed
    lock is left alone, as the sweep leaves it."""
    if names_migration(path):
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
    taken too: nothing but a migrator writes it, and the sweep, which clears
    it on a current store, cannot open this one. Both go through
    `remove_dead_merge_lock()`, which refuses a lock whose creating process
    still holds its flock. The line is printed before the removal and the
    takeover rides in the migration event."""
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
    # A current (or newer) store must reach the sweep even if a stale merge
    # lock exists, and `store.open()` judges a newer one by its floor there;
    # only a dead migrator's lock is this owner's to clear before it does.
    if migration_version(target.store_path) >= store.SCHEMA_VERSION:
        _reclaim_on_current(target, out)
        return
    target.store_path.parent.mkdir(parents=True, exist_ok=True)
    taken = take_stale_lock(target, out)
    with merge_lock(target, MIGRATION_HOLDER, operation="migration"):
        # Another owner may have stamped the store while we waited.
        version = migration_version(target.store_path)
        if version >= store.SCHEMA_VERSION:
            return
        def record(conn, from_version):
            # Inside init()'s transaction: the stamp never commits without it.
            # Only an already registered project carries the event: creating
            # its row here would refuse the operator's `project add` later.
            # init()'s `migrate` intervention records a fresh store's stamp.
            project = _project_id(conn, target)
            if project is None:
                return
            summary = {"from": from_version, "to": store.SCHEMA_VERSION}
            if taken:
                summary["staleLock"] = taken
            event(conn, project, "migration", json.dumps(summary),
                  int(time() * 1000))

        store.open(target.store_path, migrate="owner", on_migrate=record).close()
