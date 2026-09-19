"""Pin the factory's Python modules and build identity before dispatch."""
import importlib
import pkgutil
from pathlib import Path

from holophyte.gates import sh

BUILD = None


def factory_checkout():
    """The resolved checkout containing the running factory.py."""
    return Path(__file__).resolve().parents[1]


def build_sha():
    """The startup build, with a fallback for callers bypassing the CLI."""
    return BUILD or sh(['git', 'rev-parse', '--short', 'HEAD'],
                       factory_checkout())


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
