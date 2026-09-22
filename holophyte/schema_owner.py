"""Supervisor-owned schema startup, serialized with merges."""
import json
from time import time

import store
import store.read
from holophyte.gates import MergeLockHeld, merge_lock
from store.launch_backoff import event


def wait_for_migration(target, provider, stop, out):
    """Retry lock contention at startup; a long merge must not kill the owner."""
    while not stop.is_set():
        try:
            migrate_store(target, provider)
        except MergeLockHeld as exc:
            # Each acquisition already polls for the full lock wait. Preserve
            # the holder and keep the detached supervisor alive to migrate
            # on release. A stale lock with an older store needs operator
            # recovery: that store cannot yet be opened by --sweep --act.
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


def migrate_store(target, provider=None):
    """Run once at supervisor startup, including startup after exec."""
    # A current store must reach the sweep even if a stale merge lock exists.
    if migration_version(target.store_path) == store.SCHEMA_VERSION:
        return
    target.store_path.parent.mkdir(parents=True, exist_ok=True)
    with merge_lock(target, None, operation="migration"):
        # Another owner may have stamped the store while we waited.
        version = migration_version(target.store_path)
        if version == store.SCHEMA_VERSION:
            return
        conn = store.open(target.store_path, migrate="owner")
        try:
            with store.transaction(conn):
                row = conn.execute(
                    "SELECT id FROM projects WHERE repoPath = ? ORDER BY id LIMIT 1",
                    (str(target.path),)).fetchone()
                project = row[0] if row else store.ensure_project(
                    conn, provider.team if provider else str(target.path), target.path)
                event(conn, project, "migration", json.dumps({
                    "from": version, "to": store.SCHEMA_VERSION}), int(time() * 1000))
        finally:
            conn.close()
