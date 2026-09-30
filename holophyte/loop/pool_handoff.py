"""Same-process worker ownership across exec, beside the target store."""
import ast
import json
import os
from types import SimpleNamespace

from holophyte.host.startup import factory_checkout


def read(target):
    try:
        return json.loads(target.store_path.with_name("pool.json").read_text())
    except (OSError, ValueError):
        return {}


def save(target, pool, previous=None):
    previous = set(pool) if previous is None else previous
    data = {"parent": os.getpid(), "workers": [
        {"pid": pid, "slot": slot, "previous": pid in previous}
        for pid, (slot, _) in pool.items()]}
    path = target.store_path.with_name("pool.json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def restore(target):
    data = read(target)
    if data.get("parent") != os.getpid():
        return {}
    # Exec keeps pid and children: probing here would reap an exit status.
    return {w["pid"]: (w["slot"], SimpleNamespace(pid=w["pid"], returncode=None))
            for w in data.get("workers", [])}


def next_slot(pool):
    return max((slot for slot, _ in pool.values()), default=0) + 1


SCHEMA_LITERALS = ("SCHEMA_VERSION", "READABLE_FROM")


def fetched_schema(target):
    """Read without importing the arriving store; an unreadable one keeps the drain."""
    from holophyte.cli.operator import sh

    found = {}
    try:
        tree = ast.parse(sh(["git", "show", "origin/main:store/schema.py"],
                            factory_checkout()))
    except (OSError, RuntimeError, SyntaxError, ValueError):
        return None, None
    for node in tree.body:
        for name in (node.targets if isinstance(node, ast.Assign) else ()):
            if isinstance(name, ast.Name) and name.id in SCHEMA_LITERALS:
                found[name.id] = _literal(node.value, found)
    return found.get("SCHEMA_VERSION"), found.get("READABLE_FROM")


def _literal(value, found):
    if isinstance(value, ast.Name):
        return found.get(value.id)
    try:
        return ast.literal_eval(value)
    except (SyntaxError, TypeError, ValueError):
        return None


def prepare_restart(state, target, pool):
    from holophyte.cli.operator import sh

    if not state.restart or state.stopped or state.restart_reason:
        return False
    if state.prepared_sha is None:
        state.prepared_sha = sh(["git", "rev-parse", "--short", "HEAD"],
                               factory_checkout())
        state.can_ff, state.schema_changed = _prepare_reexec(target, pool)
    return not state.schema_changed


def listing(target, conn, project, provider):
    from holophyte.config.config_tables import loop_config
    from holophyte.host.supervisor import linear_budget_low
    from holophyte.loop.claim_store import FAILED, store_mode, sync_board
    from holophyte.loop.dispatch import _mirror_queue

    if store_mode(target):
        import store.read
        synced = sync_board(target, conn, project, provider,
                            min_interval_ms=loop_config(target).tick_sec * 1000)
        queue = [{"id": row.linearIdentifier}
                 for row in store.read.claimable(conn, project)]
        if linear_budget_low() or (synced == FAILED and not queue):
            return None
        return queue
    listing = _mirror_queue(target, conn, project, provider)
    return None if linear_budget_low() else listing


def workers_on_previous_build(target):
    data = read(target)
    if not data.get("parent"):
        return 0
    try:
        os.kill(data["parent"], 0)
    except (OSError, ValueError):
        return 0
    return sum(bool(worker.get("previous")) for worker in data.get("workers", []))


def _checkout_refused(exc):
    print("[holo2] re-exec: factory checkout not fast-forwarded "
          f"at {factory_checkout()} "
          f"({' '.join(str(exc).split())}); executing the code on disk", flush=True)


def _prepare_reexec(target, worker_pids):
    from holophyte.cli.operator import _fetch_main, sh
    from store.schema import SCHEMA_VERSION

    can_ff = _fetch_main(target)
    if not can_ff:
        return False, False
    version, floor = fetched_schema(target)
    # Additive: this build's live workers can still open the migrated store.
    additive = (isinstance(version, int) and isinstance(floor, int)
                and version > SCHEMA_VERSION >= floor)
    schema_moves = version != SCHEMA_VERSION and not additive
    arriving = sh(["git", "rev-parse", "--short", "origin/main"], factory_checkout())
    if additive:
        decision = (f"schema {SCHEMA_VERSION} -> {version} is additive"
                    f" (readable from {floor}); fast-forwarding to {arriving}"
                    f" under {len(worker_pids)} live worker(s)")
    elif schema_moves:
        decision = (f"schema {SCHEMA_VERSION} -> {version}; draining"
                    f" {len(worker_pids)} worker(s) before fast-forward to {arriving}")
    else:
        decision = (f"schema unchanged ({SCHEMA_VERSION}); fast-forwarding to"
                    f" {arriving} under {len(worker_pids)} live worker(s)")
    leaving = sh(["git", "rev-parse", "--short", "HEAD"], factory_checkout())
    print(f"[holo2] re-exec: {decision} (leaving {leaving})", flush=True)
    return can_ff, schema_moves


def _fetch_main(target):
    from holophyte.cli.operator import sh

    try:
        sh(["git", "fetch", "origin", "main"], factory_checkout())
        if sh(["git", "branch", "--show-current"], factory_checkout()) != "main":
            raise RuntimeError("not on main")
        if st := sh("git status --porcelain --untracked-files=no".split(),
                    factory_checkout()):
            raise RuntimeError("checkout not clean: " + ", ".join(
                line.split(maxsplit=1)[1] for line in st.splitlines()[:3]))
        return True
    except (RuntimeError, OSError) as exc:
        _checkout_refused(exc)
        return False


def _ff_main(target):
    """Best effort: a diverged checkout still executes the disk build."""
    from holophyte.cli.operator import sh

    try:
        sh(["git", "merge", "--ff-only", "origin/main"], factory_checkout())
    except (RuntimeError, OSError) as exc:
        _checkout_refused(exc)
        return False
    return True
