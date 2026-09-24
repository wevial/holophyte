"""holophyte.host: the host registry, `HOLOPHYTE_HOME/host.toml`.

One file per host lists the projects the host daemon serves and the host
sweep watches, by path; everything else about a project -- its route and
unit name (`[serve] name`), its thresholds, its store -- stays in that
project's own config and state directory. `Host.locate()` finds the file,
`Host.projects()` enumerates it, re-reading it only when its stat changed,
and `register()` / `unregister()` are the only writers, each rewriting the
file whole through an exclusive temporary file and a rename: two concurrent
writers queue on the temporary file instead of losing each other's entry,
and a crash leaves the old file or the new one.

A route name resolves through `Host.project()` only, never through a path
built from outside input. Nothing here opens a store.
"""
import dataclasses
import os
import time
import tomllib
from pathlib import Path

from holophyte.config import serve_config
from holophyte.project import DEFAULT_HOLOPHYTE_HOME, Project

HOST_FILE = "host.toml"
# The known shape of the file: a key outside it is refused, as the project
# config refuses one, so a typo is not a knob the operator believes is set.
HOST_KEYS = {"serve": {"bind", "machine_token_file", "actions"},
             "supervisor": {"sweep_sec"}}
# How long a writer waits for another writer's temporary file to go before
# it gives up naming the file: a crash mid-write is the one way it stays.
WRITE_WAIT_SEC = 10
WRITE_POLL_SEC = 0.05
TOMLKIT_MISSING = ("[holo2] project add and remove need the tomlkit module to"
                   " rewrite host.toml; install it with"
                   " python3 -m pip install --user -r requirements.txt")


class HostError(ValueError):
    """A registry that cannot be used as written, or a refused edit."""


def home():
    """The host's state directory, `HOLOPHYTE_HOME` or `~/.holophyte`."""
    return Path(os.environ.get("HOLOPHYTE_HOME")
                or DEFAULT_HOLOPHYTE_HOME).expanduser()


@dataclasses.dataclass(frozen=True)
class HostProject:
    """One registry entry: its name from its own config, its resolved path,
    the `Project` located without adoption, and why the config could not
    give a name (`name` is then None and nothing routes to it)."""

    name: str | None
    path: Path
    target: Project
    error: str | None = None


def _stamp(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_ino, stat.st_size)


def _paths(document, source):
    """The `[[project]]` paths of a parsed registry, its tables checked."""
    for table, value in document.items():
        if table == "project":
            continue
        if table not in HOST_KEYS or not isinstance(value, dict):
            raise HostError(f"[holo2] {source}: unknown table or key {table!r}")
        unknown = sorted(set(value) - HOST_KEYS[table])
        if unknown:
            raise HostError(f"[holo2] {source}: unknown [{table}] key "
                            f"{unknown[0]!r}")
    entries = document.get("project", [])
    if not isinstance(entries, list):
        raise HostError(f"[holo2] {source}: project must be [[project]] tables")
    paths = []
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or set(entry) != {"path"}):
            raise HostError(f"[holo2] {source}: each [[project]] holds one "
                            f"absolute path, got {entry!r}")
        paths.append(Path(path).resolve())
    return paths


def _entry(path):
    target = Project.locate(path, adopt=False)
    try:
        return HostProject(serve_config(target).name, path, target)
    except SystemExit as bad:
        return HostProject(None, path, target, str(bad))


class Host:
    """The registry at `home()/host.toml`, read on demand.

    `projects()` stats the file on every call and re-reads it, rebuilding
    each `Project` (and so re-reading each config), only when the stat
    moved: an add or a remove is seen at the next call without a restart.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._stamp = None
        self._table = {}
        self._projects = ()

    @classmethod
    def locate(cls, home_dir=None):
        """The registry of `home_dir`, the host's home by default; the file
        need not exist yet."""
        return cls(Path(home_dir or home()) / HOST_FILE)

    @property
    def home(self):
        return self.path.parent

    def table(self):
        """The parsed file, `{}` when there is none."""
        self._reload()
        return self._table

    def projects(self):
        """The registered projects in file order.

        Two entries whose configs give one name are refused naming both,
        the check the daemon and the sweep make at every start and reload.
        """
        self._reload()
        return self._projects

    def project(self, name):
        """The entry named `name`, None when the registry has none."""
        return next((entry for entry in self.projects()
                     if name is not None and entry.name == name), None)

    def _reload(self):
        stamp = _stamp(self.path)
        if stamp == self._stamp and stamp is not None:
            return
        text = self.path.read_text() if stamp is not None else ""
        try:
            table = tomllib.loads(text)
        except tomllib.TOMLDecodeError as bad:
            raise HostError(f"[holo2] malformed {self.path}: {bad}") from None
        entries = tuple(_entry(path) for path in _paths(table, self.path))
        _refuse_duplicates(entries, self.path)
        self._stamp, self._table, self._projects = stamp, table, entries


def _refuse_duplicates(entries, source):
    seen = {}
    for entry in entries:
        for key in (("path", entry.path), ("name", entry.name)):
            if key[1] is None:
                continue
            if key in seen:
                raise HostError(
                    f"[holo2] {source}: {entry.path} and {seen[key].path} are"
                    f" both registered as {key[0]} {key[1]}; one name and one"
                    " path per project")
            seen[key] = entry


def check_new(host, target):
    """Refuse registering `target` when its path or name is already an
    entry, naming the entry; returns the name it would register under."""
    name = serve_config(target).name
    path = Path(target.path).resolve()
    for entry in host.projects():
        if entry.path == path or entry.name == name:
            raise HostError(
                f"[holo2] {entry.name or '(no name)'} {entry.path} is already"
                f" registered in {host.path}")
    return name


def register(host, target):
    """Append `target`'s path to the registry, checked against the file as
    it stands once this writer holds the temporary file."""
    def edit(document, tomlkit):
        check_new(host, target)
        entries = document.get("project") or tomlkit.aot()
        entries.append(tomlkit.table().add("path", str(target.path)))
        document["project"] = entries
    _rewrite(host, edit)


def unregister(host, name):
    """Drop the entry named `name`; no store is touched. Returns its path."""
    removed = []

    def edit(document, tomlkit):
        entry = host.project(name)
        if entry is None:
            names = ", ".join(e.name or str(e.path) for e in host.projects())
            raise HostError(f"[holo2] no project {name!r} in {host.path}"
                            f" (registered: {names or 'none'})")
        kept = tomlkit.aot()
        for table in document.get("project", []):
            if Path(table["path"]).resolve() != entry.path:
                kept.append(table)
        document["project"] = kept
        removed.append(entry.path)
    _rewrite(host, edit)
    return removed[0]


def _rewrite(host, edit, wait=WRITE_WAIT_SEC):
    """Hold `host.toml.tmp` by exclusive create, re-read the registry, apply
    `edit(document, tomlkit)`, write the result into the held file and
    rename it over the registry. A second writer waits for the first's
    temporary file to go, so it edits the file the first one wrote."""
    try:
        import tomlkit
    except ImportError as missing:
        raise SystemExit(TOMLKIT_MISSING) from missing
    host.home.mkdir(parents=True, exist_ok=True)
    temporary = host.path.with_name(host.path.name + ".tmp")
    handle = _hold(temporary, wait)
    try:
        with os.fdopen(handle, "w") as out:
            text = host.path.read_text() if host.path.exists() else ""
            document = tomlkit.parse(text)
            edit(document, tomlkit)
            out.write(tomlkit.dumps(document))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, host.path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _hold(temporary, wait):
    deadline = time.monotonic() + wait
    while True:
        try:
            return os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                           0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise HostError(
                    f"[holo2] {temporary} is held by another registry write;"
                    " remove it if no project add or remove is running"
                ) from None
            time.sleep(WRITE_POLL_SEC)
