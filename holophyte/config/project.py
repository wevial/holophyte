import contextlib
import dataclasses
import hashlib
import os
import shutil
import sys
from pathlib import Path

from holophyte.config.locks import FileLocks
from holophyte.config.reader import load_config

DEFAULT_HOLOPHYTE_HOME = "~/.holophyte"
# Part of the store: a move that left them behind moves a truncated history.
STORE_SIDECARS = ("-wal", "-shm")


def state_dir(target):
    target = Path(target)
    home = Path(os.environ.get("HOLOPHYTE_HOME") or DEFAULT_HOLOPHYTE_HOME)
    digest = hashlib.sha1(str(target.resolve()).encode()).hexdigest()[:8]
    return home.expanduser() / f"{target.name}-{digest}"


def legacy_state_layouts(target):
    target = Path(target)
    stem = target.parent / f"{target.name}.holophyte"
    layouts = []
    if stem.is_dir():
        moves = [(path, path.name) for path in sorted(stem.iterdir())
                 if path.is_file()]
        if moves:
            layouts.append((stem, moves))
    moves = []
    for suffix in ("", *STORE_SIDECARS):
        sibling = stem.with_name(f"{stem.name}.db{suffix}")
        if sibling.is_file():
            moves.append((sibling, f"store.db{suffix}"))
    toml = stem.with_name(f"{stem.name}.toml")
    if toml.is_file():
        moves.append((toml, "config.toml"))
    if moves:
        layouts.append((None, moves))
    return layouts


def adopt_legacy_state(target, destination, out=None):
    out = sys.stdout if out is None else out
    destination = Path(destination)
    layouts = legacy_state_layouts(target)
    stores = [source for _, moves in layouts for source, name in moves
              if name == "store.db"]
    new_store = destination / "store.db"
    if len(stores) > 1 or (stores and new_store.exists()):
        standing = [str(new_store)] if new_store.exists() else []
        raise SystemExit(
            f"[holo2] {target} has more than one store: "
            + ", ".join(standing + [str(path) for path in stores])
            + "; refusing to start against one and shadow the rest -- move"
            " or remove all but the history you want to keep")
    if new_store.exists() or len(layouts) != 1:
        return []
    stem, moves = layouts[0]
    # Every landing is checked before the first move, so a refusal moves nothing.
    for source, name in moves:
        landing = destination / name
        if landing.exists():
            raise SystemExit(
                f"[holo2] cannot adopt {source}: {landing} is already there;"
                " refusing to overwrite it -- move or remove one of the two")
    destination.mkdir(parents=True, exist_ok=True)
    adopted = []
    for source, name in moves:
        landing = destination / name
        try:
            os.replace(source, landing)
        except OSError:
            # `os.replace` cannot cross filesystems; `shutil.move` can.
            shutil.move(str(source), str(landing))
        print(f"[holo2] adopted {source} -> {landing}", file=out)
        adopted.append(landing)
    if stem is not None:
        with contextlib.suppress(OSError):
            stem.rmdir()
    return adopted


@dataclasses.dataclass
class Project:
    path: Path
    holo_dir: Path
    store_path: Path
    config_path: Path
    worktrees: Path
    _config: dict | None = dataclasses.field(
        default=None, repr=False, compare=False)
    locks: object = dataclasses.field(default=None, repr=False, compare=False)

    def __post_init__(self):
        # A `FileLocks` copied by `dataclasses.replace()` names the old target.
        if self.locks is None or (isinstance(self.locks, FileLocks)
                                  and self.locks.target is not self):
            self.locks = FileLocks(self)

    @classmethod
    def locate(cls, path, adopt=True):
        path = Path(path)
        holo_dir = state_dir(path)
        # Adoption runs before anything opens a store at the new address.
        if adopt:
            adopt_legacy_state(path, holo_dir)
        return cls(
            path=path,
            holo_dir=holo_dir,
            store_path=holo_dir / "store.db",
            config_path=holo_dir / "config.toml",
            worktrees=path.parent / f"{path.name}.worktrees")

    def config(self):
        """Parsed on demand, so a malformed config fails only the target it names."""
        if self._config is None:
            self._config = load_config(self.config_path)
        return self._config


def worktree_path(target, branch):
    return target.worktrees / branch.split("/", 1)[-1]
