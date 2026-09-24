"""Explicit project registration and admission, against one store at a time.

`add` registers the project in its store and then in the host registry
(`holophyte.host`); `remove` drops the registry entry and touches no store;
`list` prints the registry with each project's admission read from its own
store, or, with `--store`, that one store's rows.
"""
import argparse
import subprocess
from pathlib import Path

import store
import store.read
from holophyte.config import check_config
from holophyte.config_tables import board_config
from holophyte.host import Host, check_new, register, unregister
from holophyte.project import Project
from holophyte.runs import open_store


def project_cli(argv):
    parser = argparse.ArgumentParser(prog="factory.py project")
    commands = parser.add_subparsers(dest="command", required=True)
    for verb in ("add", "remove", "list", "enable", "hold", "disable"):
        command = commands.add_parser(verb)
        if verb != "remove":
            command.add_argument(
                "--store", type=Path,
                help="store database (default: project/current repository)")
        if verb == "add":
            command.add_argument("path", type=Path)
        elif verb != "list":
            command.add_argument("name")
        if verb in ("enable", "hold", "disable"):
            command.add_argument("--note", required=verb != "enable",
                                 default="enabled by operator")
    args = parser.parse_args(argv)
    if hasattr(args, "note") and not args.note.strip():
        parser.error("--note must be non-empty text")
    try:
        if args.command == "remove":
            path = unregister(Host.locate(), args.name)
            print(f"[holo2] project {args.name} {path} removed from the"
                  " host registry; its store is untouched")
            return
        if args.command == "list" and args.store is None:
            _list_host(Host.locate())
            return
        path = args.path if args.command == "add" else Path.cwd()
        target = Project.locate(path.resolve())
        _run(args, target)
    except ValueError as error:
        raise SystemExit(str(error)) from None


def _run(args, target):
    host = Host.locate() if args.command == "add" else None
    settings = None
    if host is not None:
        settings = _validate(target)
        check_new(host, target)
    conn = open_store(target, args.store)
    try:
        _dispatch(conn, args, target, settings)
    finally:
        conn.close()
    if host is not None:
        register(host, target)
        print(f"[holo2] {target.path} registered in {host.path}")


def _validate(target):
    result = subprocess.run(["git", "-C", str(target.path), "rev-parse",
                             "--show-toplevel"], capture_output=True, text=True)
    if result.returncode or Path(result.stdout.strip()).resolve() != target.path:
        raise ValueError(f"not a repository root: {target.path}")
    check_config(target)
    settings = board_config(target)
    if settings is None or not settings.team.strip():
        raise ValueError(f"project {target.path} requires [board] "
                         "configuration naming a team")
    return settings


def _list_host(host):
    """One line per registry entry: name, path, admission and hold note
    from the project's own store, read-only; `-` where it has none."""
    for entry in host.projects():
        admission = note = "-"
        if entry.target.store_path.exists():
            conn = store.read.open_readonly(entry.target.store_path)
            try:
                row = conn.execute(
                    "SELECT admission, holdNote FROM projects WHERE repoPath = ?",
                    (str(entry.path),)).fetchone()
            finally:
                conn.close()
            if row is not None:
                admission = row[0]
                note = " ".join((row[1] or "-").splitlines())
        print(f"{entry.name or '-'}\t{entry.path}\t{admission}\t{note}"
              + (f"\terror={entry.error}" if entry.error else ""))


def _dispatch(conn, args, target, settings):
    if args.command == "add":
        project = store.register_project(conn, settings.team, target.path)
        admission = conn.execute("SELECT admission FROM projects WHERE id = ?",
                                 (project,)).fetchone()[0]
        print(f"project {project} {target.path.name} {target.path} {admission}")
        return
    projects = store.list_projects(conn)
    if args.command == "list":
        for _, path, state, note, run in projects:
            note = " ".join((note or "-").splitlines())
            print(f"{Path(path).name}\t{path}\t{state}\t{note or '-'}"
                  f"\trun={run or '-'}")
        return
    matches = [row for row in projects if Path(row[1]).name == args.name]
    if len(matches) != 1:
        raise ValueError(f"project {args.name!r}: expected one row, "
                         f"found {len(matches)}")
    project, path, _, _, _ = matches[0]
    state = {"enable": "enabled", "hold": "held", "disable": "disabled"}[args.command]
    store.set_admission(conn, project, state, args.note)
    print(f"[holo2] project {path} {state}: {args.note}")
