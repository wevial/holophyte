import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

from holophyte.holo.render import zone
from holophyte.host.registry import Host, home

CLIENT_FILE = "client.toml"
ENVIRONMENT = "HOLO_PROJECT"
CURRENT = "current repository"
DEFAULT = "default_project"
TIMEZONE = "timezone"
CLIENT_KEYS = frozenset((DEFAULT, TIMEZONE))
HOST_FORMS = frozenset(("--status", "--serve", "--supervise", "GET /attention"))


class Resolved(NamedTuple):
    value: str
    source: str
    shown: str


class Refused(SystemExit):
    def __init__(self, line):
        super().__init__(2)
        self.line = line


def refuse(message):
    print(message, file=sys.stderr)
    raise Refused(message)


def client_path():
    return home() / CLIENT_FILE


def client_config(path=None):
    path = client_path() if path is None else path
    try:
        with path.open("rb") as stream:
            table = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as bad:
        refuse(f"[holo2] unreadable {path}: {bad}")
    unknown = sorted(set(table) - CLIENT_KEYS)
    if unknown:
        refuse(f"[holo2] {path}: unknown key {unknown[0]!r}")
    default = table.get(DEFAULT)
    if default is not None and (not isinstance(default, str)
                                or not default.strip()):
        refuse(f"[holo2] {path}: {DEFAULT} must be a project name or path,"
               f" got {default!r}")
    zone_name = table.get(TIMEZONE)
    if zone_name is not None and (not isinstance(zone_name, str)
                                  or zone(zone_name) is None):
        refuse(f"[holo2] {path}: {TIMEZONE} must be a zone name such as"
               f" \"America/Los_Angeles\", got {zone_name!r}")
    return table


def _named(value, source, host):
    if not value.strip():
        refuse(f"[holo2] {source} is empty; give a project name or path")
    if "/" in value or os.sep in value or Path(value).is_dir():
        path = str(Path(value).expanduser().resolve())
        return Resolved(path, source, path)
    entry = host.project(value)
    if entry is not None:
        return Resolved(str(entry.path), source, f"{value} ({entry.path})")
    names = sorted(entry.name for entry in host.projects() if entry.name)
    refuse(f"[holo2] {source} names {value!r}, which {host.path} does not"
           f" register; registered names: {', '.join(names) or '(none)'}")


def _work_tree():
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True)
    except OSError:
        return None
    top = result.stdout.removesuffix("\n")
    if result.returncode != 0 or not top:
        return None
    return Path(top).resolve()


def _current(host):
    top = _work_tree()
    if top is None:
        return None
    entry = next((entry for entry in host.projects() if entry.path == top),
                 None)
    if entry is None:
        return None
    return Resolved(str(entry.path), CURRENT,
                    f"{entry.name or entry.path} ({entry.path})")


def resolve(option, default, host=None):
    host = Host.locate() if host is None else host
    if option is not None:
        return _named(option, "-p", host)
    if os.environ.get(ENVIRONMENT):
        return _named(os.environ[ENVIRONMENT], ENVIRONMENT, host)
    return _current(host) or (
        _named(default, f"{DEFAULT} in {client_path()}", host)
        if default else None)


def none_found(words):
    return (f"[holo2] holo {' '.join(words)} needs a project and no source"
            f" names one: -p NAME|PATH, {ENVIRONMENT}, the {CURRENT}"
            f" ({Path.cwd()} is in no registered work tree) and {DEFAULT} in"
            f" {client_path()}")
