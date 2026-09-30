import importlib
import pkgutil
from pathlib import Path

from holophyte import media_store
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import sh

BUILD = None


def factory_checkout():
    return Path(__file__).resolve().parents[2]


def build_sha():
    return BUILD or sh(['git', 'rev-parse', '--short', 'HEAD'],
                       factory_checkout())


def eager_import():
    global BUILD
    BUILD = build_sha()
    for name in ('holophyte', 'store'):
        package = importlib.import_module(name)
        for module in pkgutil.walk_packages(package.__path__, name + '.'):
            importlib.import_module(module.name)
    root = Path(__file__).resolve().parents[2]
    for module in pkgutil.iter_modules([str(root)]):
        if not module.ispkg:
            importlib.import_module(module.name)


def banner(target=None):
    print(f'[holo2] factory at {build_sha()}', flush=True)
    merge = merge_config(target) if target is not None else None
    if merge and not merge.mention_accounts and merge.human_threads == "act":
        print("[holo2] mentions are open to any account "
              "([merge] mention_accounts is empty)", flush=True)
    if merge and merge.media_bucket:
        try:
            media_store.credentials()
        except media_store.MissingCredentials as error:
            print(f'[holo2] warning: media_bucket is configured but {error.name} '
                  'is not set; evidence will not be published', flush=True)
