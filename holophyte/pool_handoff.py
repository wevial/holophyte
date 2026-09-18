"""Same-process worker ownership across exec, beside the target store."""
import ast
import json
import os
from types import SimpleNamespace

from store.schema import SCHEMA_VERSION


def read(target):
    try:
        return json.loads(target.store_path.with_name("pool.json").read_text())
    except (OSError, ValueError):
        return {}


def save(target, pool, failed=False, previous=None):
    previous = set(pool) if previous is None else previous
    data = {"parent": os.getpid(), "failed": failed, "workers": [
        {"pid": pid, "slot": slot, "previous": pid in previous}
        for pid, (slot, _) in pool.items()]}
    path = target.store_path.with_name("pool.json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def restore(target):
    data = read(target)
    if data.get("parent") != os.getpid():
        return {}, False
    # exec preserves both pid and child ownership, including exited children
    # awaiting waitpid. Do not probe/reap them here and lose their exit status.
    return {w["pid"]: (w["slot"], SimpleNamespace(pid=w["pid"], returncode=None))
            for w in data.get("workers", [])}, data.get("failed", False)


def next_slot(pool):
    return max((slot for slot, _ in pool.values()), default=0) + 1


def _schema_changed(target):
    """Read the arriving constant without importing or migrating its store.

    An unreadable/unknown version conservatively keeps the existing drain.
    """
    try:
        tree = ast.parse((target.path / "store" / "schema.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(name, ast.Name) and name.id == "SCHEMA_VERSION"
                    for name in node.targets):
                return ast.literal_eval(node.value) != SCHEMA_VERSION
    except (OSError, SyntaxError, ValueError):
        pass
    return True


def prepare_restart(state, target):
    """Update once, then decide against the exact tree exec will load."""
    from holophyte.operator import _fast_forward_checkout, sh

    if not state.restart or state.stopped or state.restart_reason:
        return False
    if state.prepared_sha is None:
        state.prepared_sha = sh(["git", "rev-parse", "--short", "HEAD"],
                               target.path)
        _fast_forward_checkout(target)
        state.schema_changed = _schema_changed(target)
    return not state.schema_changed


def listing(target, conn, project, provider):
    from holophyte.dispatch import _mirror_queue
    from holophyte.supervisor import linear_budget_low

    listing = _mirror_queue(target, conn, project, provider)
    return None if linear_budget_low() else listing


def workers_on_previous_build(target):
    """Inherited workers still owned by the running scheduler."""
    data = read(target)
    if not data.get("parent"):
        return 0
    try:
        os.kill(data["parent"], 0)
    except (OSError, ValueError):
        return 0
    return sum(bool(worker.get("previous")) for worker in data.get("workers", []))
