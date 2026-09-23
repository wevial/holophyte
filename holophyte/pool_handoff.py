"""Same-process worker ownership across exec, beside the target store."""
import ast
import json
import os
from types import SimpleNamespace

from holophyte.startup import factory_checkout


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
    # exec preserves both pid and child ownership, including exited children
    # awaiting waitpid. Do not probe/reap them here and lose their exit status.
    return {w["pid"]: (w["slot"], SimpleNamespace(pid=w["pid"], returncode=None))
            for w in data.get("workers", [])}


def next_slot(pool):
    return max((slot for slot, _ in pool.values()), default=0) + 1


# The arriving schema's two literals the restart decision reads.
SCHEMA_LITERALS = ("SCHEMA_VERSION", "READABLE_FROM")


def fetched_schema(target):
    """Read the arriving `(SCHEMA_VERSION, READABLE_FROM)` without importing
    or migrating its store.

    Each is a literal, or for `READABLE_FROM` the name of the other; one
    that is missing or unreadable is None, which conservatively keeps the
    existing drain.
    """
    from holophyte.operator import sh

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
    if isinstance(value, ast.Name):  # `READABLE_FROM = SCHEMA_VERSION`
        return found.get(value.id)
    try:
        return ast.literal_eval(value)
    except (SyntaxError, TypeError, ValueError):
        return None


def prepare_restart(state, target, pool):
    """Fetch once and defer checkout movement until any schema drain ends."""
    from holophyte.operator import sh

    if not state.restart or state.stopped or state.restart_reason:
        return False
    if state.prepared_sha is None:
        state.prepared_sha = sh(["git", "rev-parse", "--short", "HEAD"],
                               factory_checkout())
        state.can_ff, state.schema_changed = _prepare_reexec(target, pool)
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


def _checkout_refused(exc):
    print("[holo2] re-exec: factory checkout not fast-forwarded "
          f"at {factory_checkout()} "
          f"({' '.join(str(exc).split())}); executing the code on disk", flush=True)


def _prepare_reexec(target, worker_pids):
    from holophyte.operator import _fetch_main, sh
    from store.schema import SCHEMA_VERSION

    can_ff = _fetch_main(target)
    if not can_ff:
        return False, False
    version, floor = fetched_schema(target)
    # Additive: the live workers of this build can still open the store the
    # arriving build migrates, so they are handed to it rather than drained.
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
    """Fetch even with live workers or a checkout that cannot fast-forward."""
    from holophyte.operator import sh

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
    """Best effort: a diverged checkout still executes the disk build.

    Returns whether the checkout now holds origin/main."""
    from holophyte.operator import sh

    try:
        sh(["git", "merge", "--ff-only", "origin/main"], factory_checkout())
    except (RuntimeError, OSError) as exc:
        _checkout_refused(exc)
        return False
    return True
