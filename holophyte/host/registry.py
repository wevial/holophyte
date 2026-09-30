import collections
import contextlib
import dataclasses
import os
import time
import tomllib
from pathlib import Path

from holophyte.config.config_tables import board_config, board_mode, split_address
from holophyte.config.project import DEFAULT_HOLOPHYTE_HOME, Project
from holophyte.config.serve_settings import serve_config
from holophyte.config.worktree_settings import ENV_NAME, process_value
from holophyte.redact import values_held

HOST_FILE = "host.toml"
# Any other key is refused, so a typo is not a knob the operator believes set.
HOST_KEYS = {"serve": {"bind", "machine_token_file", "actions"},
             "supervisor": {"sweep_sec"}, "console": {"daemons"}}
SWEEP_SEC = 60
HostSettings = collections.namedtuple(
    "HostSettings", ("bind", "machine_token_file", "actions", "sweep_sec",
                     "daemons"))
# A crash mid-write is the one way another writer's temporary file stays.
WRITE_WAIT_SEC = 10
WRITE_POLL_SEC = 0.05
UNIT_TARGET_KEYS = frozenset(("HOLOPHYTE_TARGET", "HOLOPHYTE_SERVE_ADDRESS",
                              "HOLOPHYTE_SERVE_PORT"))
TOMLKIT_MISSING = ("[holo2] project add and remove need the tomlkit module to"
                   " rewrite host.toml; install it with"
                   " python3 -m pip install --user -r requirements.txt")


class HostError(ValueError):
    pass


def home():
    return Path(os.environ.get("HOLOPHYTE_HOME")
                or DEFAULT_HOLOPHYTE_HOME).expanduser()


@dataclasses.dataclass(frozen=True)
class HostProject:
    name: str | None
    path: Path
    target: Project
    error: str | None = None


def unit_environment(text):
    values = {}
    for line in text.splitlines():
        name, separator, value = line.strip().partition("=")
        name, value = name.strip(), process_value(value.strip())
        if separator and ENV_NAME.fullmatch(name) and "\0" not in value:
            values[name] = value
    return values


@contextlib.contextmanager
def loop_unit_environment(target):
    path = home() / serve_config(target).name / "serve.env"
    try:
        values = unit_environment(path.read_text())
    except FileNotFoundError:
        values = {}
    credential = (target.config().get("agents") or {}).get(
        "implementer_credential")
    kept = {credential.get("env")} if isinstance(credential, dict) else set()
    saved = {}
    with values_held(values.values()):
        try:
            for name, value in values.items():
                if name not in UNIT_TARGET_KEYS or name in kept:
                    saved[name] = os.environ.get(name)
                    os.environ[name] = value
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _stamp(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_ino, stat.st_size)


def _paths(document, source):
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
        # A config that gives no name is this entry's error, never the registry's.
        return HostProject(None, path, target, str(bad))
    except Exception as bad:
        return HostProject(None, path, target,
                           f"{target.config_path}: {type(bad).__name__}: {bad}")


def settings(host):
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
    def __init__(self, path):
        self.path = Path(path)
        # Swapped whole: a request thread never pairs two reloads' table and entries.
        self._state = (None, {}, ())

    @classmethod
    def locate(cls, home_dir=None):
        return cls(Path(home_dir or home()) / HOST_FILE)

    @property
    def home(self):
        return self.path.parent

    def table(self):
        return self._current()[1]

    def projects(self):
        return self._current()[2]

    def project(self, name):
        return next((entry for entry in self.projects()
                     if name is not None and entry.name == name), None)

    def _current(self):
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
        # Stamped before parsing, so a config edited mid-rebuild is seen next call.
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
    host = Host.locate() if host is None else host
    if not host.path.exists():
        return None
    path = Path(target.path).resolve()
    return host.path if any(entry.path == path
                            for entry in host.projects()) else None


def watched_line(target):
    try:
        registry = registry_of(target)
    except HostError as bad:
        # Unreadable cannot rule out the host sweep: two watchers fake strikes.
        return f"{bad}; no supervisor for {target.path} beside it"
    if registry is None:
        return None
    return (f"[holo2] the host sweep watches {target.path}, registered in"
            f" {registry}; no supervisor started for it")


def already_registered(host, entry):
    return HostError(f"[holo2] {entry.name or '(no name)'} {entry.path} is"
                     f" already registered in {host.path}")


def registered_at(host, target):
    path = Path(target.path).resolve()
    return next((entry for entry in host.projects() if entry.path == path),
                None)


def check_new(host, target):
    name = serve_config(target).name
    path = Path(target.path).resolve()
    for entry in host.projects():
        if entry.path == path or entry.name == name:
            raise already_registered(host, entry)
    return name


def native_key_conflict(target, host=None):
    key = _native_key(target)
    if key is None:
        return None
    host = Host.locate() if host is None else host
    try:
        entries = host.projects()
    except HostError as bad:
        return f"{bad}; cannot check [board] prefix {key} against it"
    path = Path(target.path).resolve()
    entries = [entry for entry in entries if entry.error is None]
    for entry in entries:
        if entry.path != path and _native_key(entry.target) == key:
            return (f"[holo2] [board] prefix {key} of {path} is already the"
                    f" native board prefix of {entry.path}")
    targets = [entry.target for entry in entries]
    if path not in (entry.path for entry in entries):
        targets.append(target)
    for other in targets:
        identifier = _linear_identifier(other, key)
        if identifier is not None:
            return (f"[holo2] [board] prefix {key} of {path} is a Linear team"
                    f" key on this host: the store of {other.path} holds"
                    f" {identifier}")
    return None


def _native_key(target):
    try:
        if board_mode(target).kind != "native":
            return None
        return board_config(target).prefix
    except (SystemExit, Exception):
        return None


def _linear_identifier(target, key):
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
    def edit(document, tomlkit):
        check_new(host, target)
        entries = document.get("project") or tomlkit.aot()
        entries.append(tomlkit.table().add("path", str(target.path)))
        document["project"] = entries
    _rewrite(host, edit)


def unregister(host, key):
    removed = []

    def edit(document, tomlkit):
        # Not `projects()`: a remove repairs the duplicates `projects()` refuses.
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
    try:
        import tomlkit
    except ImportError as missing:
        raise SystemExit(TOMLKIT_MISSING) from missing
    host.home.mkdir(parents=True, exist_ok=True)
    temporary = host.path.with_name(host.path.name + ".tmp")
    # Held by exclusive create: a second writer waits, then edits the first's file.
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
