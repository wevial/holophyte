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
built from outside input; `registry_of()` answers whether a project is the
host sweep's to watch. `settings()` is the file's own keys, typed:
`[serve] bind`, `machine_token_file` and `actions`, `[console] daemons`,
`[supervisor] sweep_sec`. `native_key_conflict()` alone opens stores, each
read-only: a native board's `KEY` is its own on the host (KO-752).
"""
import collections
import dataclasses
import os
import time
import tomllib
from pathlib import Path

from holophyte.config import serve_config
from holophyte.config_tables import board_config, board_mode, split_address
from holophyte.project import DEFAULT_HOLOPHYTE_HOME, Project

HOST_FILE = "host.toml"
# The known shape of the file: a key outside it is refused, as the project
# config refuses one, so a typo is not a knob the operator believes is set.
HOST_KEYS = {"serve": {"bind", "machine_token_file", "actions"},
             "supervisor": {"sweep_sec"}, "console": {"daemons"}}
# The host sweep's interval when `[supervisor] sweep_sec` is absent: the
# timer's `OnUnitActiveSec`.
SWEEP_SEC = 60
HostSettings = collections.namedtuple(
    "HostSettings", ("bind", "machine_token_file", "actions", "sweep_sec",
                     "daemons"))
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
    """The entry for `path`; a config that cannot give a name -- refused as
    written, unreadable, not UTF-8 -- is this entry's `error`, never the
    registry's, so one bad project does not hide the others."""
    target = Project.locate(path, adopt=False)
    try:
        return HostProject(serve_config(target).name, path, target)
    except SystemExit as bad:
        return HostProject(None, path, target, str(bad))
    except Exception as bad:
        return HostProject(None, path, target,
                           f"{target.config_path}: {type(bad).__name__}: {bad}")


def settings(host):
    """The registry's own keys over their defaults; HostError naming the
    key when one is the wrong shape. `machine_token_file` is a path, `~`
    expanded, a relative one taken against the home."""
    table = host.table()
    serve = table.get("serve", {})
    bind = serve.get("bind")
    if bind is not None and (not isinstance(bind, str) or not bind.strip()):
        raise HostError(f"[holo2] {host.path}: [serve] bind must be"
                        f" PORT or HOST:PORT, got {bind!r}")
    token = serve.get("machine_token_file")
    if token is not None:
        if not isinstance(token, str) or not token.strip():
            raise HostError(f"[holo2] {host.path}: [serve] machine_token_file"
                            f" must be a non-empty path, got {token!r}")
        token = host.home / Path(token).expanduser()
    actions = serve.get("actions", False)
    if not isinstance(actions, bool):
        raise HostError(f"[holo2] {host.path}: [serve] actions must be true"
                        f" or false, got {actions!r}")
    sweep = table.get("supervisor", {}).get("sweep_sec", SWEEP_SEC)
    if isinstance(sweep, bool) or not isinstance(sweep, int) or sweep <= 0:
        raise HostError(f"[holo2] {host.path}: [supervisor] sweep_sec must be"
                        f" a positive whole number of seconds, got {sweep!r}")
    return HostSettings(bind, token, actions, sweep,
                        _daemons(table.get("console", {}), host.path))


def _daemons(table, source):
    daemons = table.get("daemons", [])
    if not isinstance(daemons, list):
        raise HostError(f"[holo2] {source}: [console] daemons must be a list")
    for entry in daemons:
        try:
            if not isinstance(entry, str):
                raise ValueError(f"expected HOST:PORT, got {entry!r}")
            split_address(entry)
        except ValueError as bad:
            raise HostError(f"[holo2] {source}: [console] daemons: {bad}"
                            ) from None
    if len(set(daemons)) != len(daemons):
        raise HostError(f"[holo2] {source}: [console] daemons lists an"
                        " address twice")
    return tuple(daemons)


class Host:
    """The registry at `home()/host.toml`, read on demand.

    `projects()` stats the file on every call and re-reads it, rebuilding
    each `Project` (and so re-reading each config), only when the stat
    moved: an add or a remove is seen at the next call without a restart.
    """

    def __init__(self, path):
        self.path = Path(path)
        # `(stamp, table, projects)`, swapped whole by one assignment so a
        # request thread never pairs one reload's table with another's
        # projects under the daemon's threading server.
        self._state = (None, {}, ())

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
        return self._current()[1]

    def projects(self):
        """The registered projects in file order.

        Two entries whose configs give one name are refused naming both,
        the check the daemon and the sweep make at every start and reload.
        """
        return self._current()[2]

    def project(self, name):
        """The entry named `name`, None when the registry has none."""
        return next((entry for entry in self.projects()
                     if name is not None and entry.name == name), None)

    def _current(self):
        # The stamp covers each registered project's config too: a hand edit
        # (a native move, a mode flip) rebuilds that entry's `Project`. A
        # rebuild stamps the new entries' configs before parsing them, so an
        # edit made mid-rebuild is not lost.
        state = self._state
        registry = _stamp(self.path)
        stamp = (registry, tuple(_stamp(entry.target.config_path)
                                 for entry in state[2]))
        if stamp == state[0] and registry is not None:
            return state
        try:
            text = (self.path.read_text(encoding="utf-8")
                    if registry is not None else "")
            table = tomllib.loads(text)
        except (OSError, UnicodeDecodeError,
                tomllib.TOMLDecodeError) as bad:
            raise HostError(f"[holo2] unreadable {self.path}: {bad}") from None
        paths = _paths(table, self.path)
        configs = tuple(_stamp(Project.locate(path, adopt=False).config_path)
                        for path in paths)
        entries = tuple(_entry(path) for path in paths)
        _refuse_duplicates(entries, self.path)
        state = ((registry, configs), table, entries)
        self._state = state
        return state


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


def registry_of(target, host=None):
    """The registry file that lists `target`'s path, None when none does or
    there is no registry; HostError when the registry cannot be read. The
    loop's supervisor spawn and a hand `PROJECT --supervise` ask it: a
    registered project is the host sweep's to watch."""
    host = Host.locate() if host is None else host
    if not host.path.exists():
        return None
    path = Path(target.path).resolve()
    return host.path if any(entry.path == path
                            for entry in host.projects()) else None


def watched_line(target):
    """None when the registry does not list `target`; otherwise the line
    saying the host sweep watches it, which refuses a hand `PROJECT
    --supervise` and stops the loop's supervisor spawn. A registry that
    cannot be read answers with its error: it cannot say the host sweep is
    not watching, and two watchers on one store would manufacture the second
    strike the two-strike rule demands."""
    try:
        registry = registry_of(target)
    except HostError as bad:
        return f"{bad}; no supervisor for {target.path} beside it"
    if registry is None:
        return None
    return (f"[holo2] the host sweep watches {target.path}, registered in"
            f" {registry}; no supervisor started for it")


def already_registered(host, entry):
    """The refusal naming `entry` as already in the registry."""
    return HostError(f"[holo2] {entry.name or '(no name)'} {entry.path} is"
                     f" already registered in {host.path}")


def registered_at(host, target):
    """The entry registered at `target`'s resolved path, None when none."""
    path = Path(target.path).resolve()
    return next((entry for entry in host.projects() if entry.path == path),
                None)


def check_new(host, target):
    """Refuse registering `target` when its path or name is already an
    entry, naming the entry; returns the name it would register under."""
    name = serve_config(target).name
    path = Path(target.path).resolve()
    for entry in host.projects():
        if entry.path == path or entry.name == name:
            raise already_registered(host, entry)
    return name


def native_key_conflict(target, host=None):
    """The first reason `target`'s native `[board] key` is not its own on
    the host, as one line; None when it is, when `target` is not native,
    and when its table cannot be read: the table's own reader refuses that.

    Another registry entry whose native board has the same key conflicts,
    and so does any registered store -- `target`'s own included, registered
    or not -- holding a Linear ticket `KEY-n`: one whose board id is not
    its identifier, as a native ticket's is. An entry whose config or store
    cannot be read is skipped, as `project list` skips it.
    """
    key = _native_key(target)
    if key is None:
        return None
    host = Host.locate() if host is None else host
    try:
        entries = host.projects()
    except HostError as bad:
        return f"{bad}; cannot check [board] key {key} against it"
    path = Path(target.path).resolve()
    entries = [entry for entry in entries if entry.error is None]
    for entry in entries:
        if entry.path != path and _native_key(entry.target) == key:
            return (f"[holo2] [board] key {key} of {path} is already the"
                    f" native board key of {entry.path}")
    targets = [entry.target for entry in entries]
    if path not in (entry.path for entry in entries):
        targets.append(target)
    for other in targets:
        identifier = _linear_identifier(other, key)
        if identifier is not None:
            return (f"[holo2] [board] key {key} of {path} is a Linear team"
                    f" key on this host: the store of {other.path} holds"
                    f" {identifier}")
    return None


def _native_key(target):
    """`target`'s native `[board] key`, None when it has none or its
    config cannot be read."""
    try:
        if board_mode(target).kind != "native":
            return None
        return board_config(target).key
    except (SystemExit, Exception):
        return None


def _linear_identifier(target, key):
    """A `KEY-n` identifier of a Linear ticket in `target`'s store, None
    when it holds none or cannot be read."""
    import store.read
    if not target.store_path.exists():
        return None
    try:
        conn = store.read.open_readonly(target.store_path)
        try:
            row = conn.execute(
                "SELECT linearIdentifier FROM tickets"
                " WHERE linearIdentifier GLOB ?"
                " AND linearIssueId <> linearIdentifier"
                " ORDER BY id LIMIT 1", (f"{key}-[0-9]*",)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    return row and row[0]


def register(host, target):
    """Append `target`'s path to the registry, checked against the file as
    it stands once this writer holds the temporary file."""
    def edit(document, tomlkit):
        check_new(host, target)
        entries = document.get("project") or tomlkit.aot()
        entries.append(tomlkit.table().add("path", str(target.path)))
        document["project"] = entries
    _rewrite(host, edit)


def unregister(host, key):
    """Drop the entry whose `[serve] name` or registered path is `key`; no
    store is touched. Returns the entry's `(name, path)`, name None when
    its config gives none.

    The entry is found in the file as parsed here, each config read on its
    own, never through `projects()`: an entry whose config cannot give a
    name, or two entries that give one name, are what a remove repairs, and
    `projects()` refuses the second. A key matching two entries is refused
    naming both, so the operator names the path."""
    removed = []

    def edit(document, tomlkit):
        entries = [_entry(Path(table["path"]).resolve())
                   for table in document.get("project", [])]
        path = Path(key).expanduser().resolve()
        matches = {entry.path: entry for entry in entries
                   if entry.name == key or entry.path == path}
        if not matches:
            names = ", ".join(e.name or str(e.path) for e in entries)
            raise HostError(f"[holo2] no project {key!r} in {host.path}"
                            f" (registered: {names or 'none'})")
        if len(matches) > 1:
            raise HostError(f"[holo2] {key} matches "
                            + " and ".join(map(str, matches))
                            + f" in {host.path}; remove one by its path")
        entry, = matches.values()
        kept = tomlkit.aot()
        for table in document.get("project", []):
            if Path(table["path"]).resolve() != entry.path:
                kept.append(table)
        document["project"] = kept
        removed.append((entry.name, entry.path))
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
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            text = (host.path.read_text(encoding="utf-8")
                    if host.path.exists() else "")
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
