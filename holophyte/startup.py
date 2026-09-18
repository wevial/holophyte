"""Pin the factory's Python modules and build identity before dispatch."""
import importlib
import os
import pkgutil
from pathlib import Path

from holophyte.gates import sh

BUILD = None


def build_sha():
    """The startup build, with a fallback for callers bypassing the CLI."""
    return BUILD or sh(['git', 'rev-parse', '--short', 'HEAD'],
                       Path(__file__).resolve().parents[1])


def eager_import():
    """Load the packages and root modules before a checkout can mix builds."""
    global BUILD
    BUILD = build_sha()
    for name in ('holophyte', 'store'):
        package = importlib.import_module(name)
        for module in pkgutil.walk_packages(package.__path__, name + '.'):
            importlib.import_module(module.name)
    # Board calls also lazily import root modules such as linear_provider.
    root = Path(__file__).resolve().parents[1]
    for module in pkgutil.iter_modules([str(root)]):
        if not module.ispkg:
            importlib.import_module(module.name)


def banner():
    print(f'[holo2] factory at {build_sha()}', flush=True)


def _worker_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # An inaccessible process is still alive.
    return True


def checkout_blocked(worker_pids, draining):
    """A non-draining pool keeps its live workers on their startup build."""
    live = sum(_worker_alive(pid) for pid in worker_pids)
    if live and not draining:
        print(f"[holo2] checkout not fast-forwarded: {live} worker(s) still on"
              f" {build_sha()}", flush=True)
        return True
    return False
