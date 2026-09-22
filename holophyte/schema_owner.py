"""Supervisor-owned schema startup, serialized with merges."""
import json
from time import time

import store
import store.read
from holophyte.gates import merge_lock
from store.launch_backoff import event


def migrate_store(target, provider=None):
    """Run once at supervisor startup, including startup after exec."""
    target.store_path.parent.mkdir(parents=True, exist_ok=True)
    with merge_lock(target, None, operation="migration"):
        version = 0
        if target.store_path.exists():
            conn = store.read.open_readonly(target.store_path)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
        conn = store.open(target.store_path, migrate="owner")
        try:
            if version == store.SCHEMA_VERSION:
                return
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
