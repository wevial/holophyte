"""Where a target's state lives, and the `Target` value that carries it.

`HOLOPHYTE_HOME/<basename>-<hash>` is the address; `Target.locate()` derives
it, adopts whatever a pre-home layout left beside the checkout, and hands the
loop one value holding every path a run works against. Nothing here knows
the loop, the gates or the store.

First slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports `Target` back for its remaining call sites.
"""
import contextlib
import dataclasses
import hashlib
import os
import shutil
import sys
from pathlib import Path

from holophyte.config import load_config

DEFAULT_HOLOPHYTE_HOME = "~/.holophyte"
# The sidecars SQLite keeps beside a WAL-mode database. They are part of the
# store, so a move that left them behind would move a truncated history.
STORE_SIDECARS = ("-wal", "-shm")


def state_dir(target):
    """Where everything the factory knows about `target` lives.

    `HOLOPHYTE_HOME/<basename>-<hash>`, defaulting to `~/.holophyte`. Host
    state, not repo state: what is kept here is this host's agent routes,
    leases and heartbeats, so it belongs to the host rather than to a
    checkout that gets cloned, moved and deleted. One home also gives
    `--serve` and the drawer a single place to enumerate a host's targets,
    leaves project parents such as `/path/to` free of dotted artifacts, and
    works when that parent is not writable. The hash of the absolute path is
    what keeps `/a/repo` and `/b/repo` -- two repositories, two histories --
    out of each other's store.
    """
    target = Path(target)
    home = Path(os.environ.get("HOLOPHYTE_HOME") or DEFAULT_HOLOPHYTE_HOME)
    digest = hashlib.sha1(str(target.resolve()).encode()).hexdigest()[:8]
    return home.expanduser() / f"{target.name}-{digest}"


def legacy_state_layouts(target):
    """The pre-home addresses for `target`'s state, as moves into a new dir.

    Two of them ever existed: KO-165's `<target>.holophyte/` directory, and
    before it a family of dotted siblings (`<target>.holophyte.db` with its
    WAL sidecars, `<target>.holophyte.toml`). Each layout is returned as
    `(directory_to_remove, [(source, name_in_state_dir), ...])`.
    """
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
    """Move `target`'s legacy state into `destination`, once, loudly.

    KO-165 moved the store's address and shipped no migration with it: the
    next run on the writer host opened an empty database at the new path and shadowed
    fifteen runs, the ticket's failure count, every intervention row and the
    `[agents] implementer` route the old config carried. Nothing was lost and
    nothing said so, which is the failure this function exists to make
    impossible -- either the history moves with the address, or the factory
    refuses to start against half of it.

    What makes it a one-time event is the store at the new address, not the
    directory holding it: an operator who writes `config.toml` at the new
    address first -- which the README tells them to do -- creates that
    directory without adopting anything, and gating on the directory would
    leave the legacy history for the empty store `open_store()` writes a
    moment later to shadow. So adoption runs whenever `destination` has no
    store, merging into the directory if it is already there, and a file
    already sitting at a landing address stops the whole move rather than
    being overwritten. Once the store has moved, whatever else is lying
    beside the checkout is somebody's backup, not this run's state.
    """
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
    # Every landing address is checked before the first move, so a refusal
    # leaves both layouts whole rather than half of one in each place.
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
            # A home on a different filesystem from the project parent is
            # ordinary; `os.replace` cannot cross that line and `shutil.move`
            # can.
            shutil.move(str(source), str(landing))
        print(f"[holo2] adopted {source} -> {landing}", file=out)
        adopted.append(landing)
    if stem is not None:
        with contextlib.suppress(OSError):
            stem.rmdir()
    return adopted


@dataclasses.dataclass
class Target:
    """The repository a run works against, and the paths derived from it.

    Built once by `cli()` for whatever the command line names and passed to
    every function that needs one; nothing in this module remembers a target
    between calls, so two targets can live in one process (the `serve` daemon
    and the supervisor both want that) and importing this module has no
    target-specific side effect. The fields are plain paths; `config()` is the
    one accessor that does I/O, and it parses `config_path` once per instance.
    `{}` is the documented normal case for a config: every knob the file can
    set has a hardcoded default, so an absent file is exactly the default
    behavior.
    """

    path: Path
    holo_dir: Path
    store_path: Path
    config_path: Path
    worktrees: Path
    _config: dict | None = dataclasses.field(
        default=None, repr=False, compare=False)

    @classmethod
    def locate(cls, path, adopt=True):
        """The `Target` for the repository at `path`, with its state located.

        Called by `cli()` for whatever the command line names; nothing else
        derives these paths, so a caller that wants a different target builds
        another `Target` here instead of patching one path and leaving the
        other two pointing at the last one. The config is derived from the
        target too, so it lives on the value and moves with it.

        `adopt=False` derives the paths and nothing else. Adoption is a side
        effect the caller asks for: a value built for a target nobody is about
        to run against -- a daemon enumerating a host's targets, a test naming
        a directory -- must not move that target's state, and where the target
        has two stores must not exit. The same rule `config()` follows, and
        the reason importing this module builds no `Target` at all: nothing
        target-specific happens before `cli()` has picked a target.
        """
        path = Path(path)
        # Everything the factory keeps about a target lives in one directory
        # under the host's home, `HOLOPHYTE_HOME/SLUG/`, created the first
        # time something has to write there. Not inside the target: the
        # factory's own .gitignore says nothing about the target checkout, so
        # a store written into the target would leave the database and its
        # two WAL sidecars untracked in whatever repo the loop is working on
        # -- dirt a task's `git add -A` could sweep into a commit. Not beside
        # it either: see `state_dir()`.
        holo_dir = state_dir(path)
        # Whatever a previous layout left beside the checkout moves in here
        # now, before anything opens a store at the new address and finds it
        # empty.
        if adopt:
            adopt_legacy_state(path, holo_dir)
        return cls(
            path=path,
            holo_dir=holo_dir,
            # The loop's durable state: one WAL-mode SQLite file per target.
            store_path=holo_dir / "store.db",
            # Config for a target is not a file the target has to carry
            # either.
            config_path=holo_dir / "config.toml",
            # The worktree directory predates the state directory and is
            # heavy git state rather than factory state; it keeps its own
            # sibling address.
            worktrees=path.parent / f"{path.name}.worktrees")

    def config(self):
        """The target's parsed config, read once per `Target`.

        Read on demand rather than by `locate()`: parsing there would make a
        malformed `config.toml` an error for every value built, including one
        built for a target the run is not pointed at. Nothing that reads
        config runs before `cli()` picks a target, and `cli()` reads it as
        soon as it has one, so the file a run actually depends on is still
        parsed at startup: a malformed one aborts before a ticket is claimed,
        not in the middle of a round.
        """
        if self._config is None:
            self._config = load_config(self.config_path)
        return self._config
