import argparse
import subprocess
from pathlib import Path

import store
import store.read
from holophyte.admission import project_of
from holophyte.config.checks import check_config
from holophyte.config.config_tables import board_config
from holophyte.config.project import Project
from holophyte.host.registry import (
    Host,
    already_registered,
    check_new,
    native_key_conflict,
    register,
    registered_at,
    unregister,
)
from holophyte.loop.runs import open_store


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
        elif verb == "remove":
            command.add_argument("name", metavar="NAME|PATH")
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
            name, path = unregister(Host.locate(), args.name)
            print(f"[holo2] project {name or '-'} {path} removed from the"
                  " host registry; its store is untouched")
            return
        if args.command == "list" and args.store is None:
            return _list_host(Host.locate())
        path = args.path if args.command == "add" else Path.cwd()
        target = Project.locate(path.resolve())
        _run(args, target)
    except ValueError as error:
        raise SystemExit(str(error)) from None


def _run(args, target):
    host = Host.locate() if _host_add(args, target) else None
    settings = _validate(target) if args.command == "add" else None
    entry = registered_at(host, target) if host is not None else None
    if host is not None and entry is None:
        check_new(host, target)
    conn = open_store(target, args.store)
    try:
        if entry is not None and project_of(conn, target) is not None:
            raise already_registered(host, entry)
        _dispatch(conn, args, target, settings)
    finally:
        conn.close()
    if entry is not None:
        print(f"[holo2] {target.path} is already registered in {host.path};"
              " wrote its missing store row, host.toml unchanged")
    elif host is not None:
        register(host, target)
        print(f"[holo2] {target.path} registered in {host.path}")
    elif args.command == "add":
        print(f"[holo2] {target.path} registered in {args.store} only; the"
              f" host registry reads its store at {target.store_path}, so"
              " host.toml is untouched")


def _host_add(args, target):
    return args.command == "add" and (
        args.store is None
        or args.store.resolve() == target.store_path.resolve())


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
    conflict = native_key_conflict(target)
    if conflict is not None:
        raise ValueError(conflict)
    return settings


def _list_host(host):
    failed = False
    for entry in host.projects():
        admission = note = "-"
        error = entry.error
        try:
            row = _admission(entry)
        except Exception as bad:
            row, error = None, error or f"{type(bad).__name__}: {bad}"
        if row is not None:
            admission = row[0]
            note = " ".join((row[1] or "-").splitlines())
        failed = failed or error is not None
        print(f"{entry.name or '-'}\t{entry.path}\t{admission}\t{note}"
              + (f"\terror={error}" if error else ""))
    return 1 if failed else 0


def _admission(entry):
    if not entry.target.store_path.exists():
        return None
    conn = store.read.open_readonly(entry.target.store_path)
    try:
        project = project_of(conn, entry.target)
        if project is None:
            return None
        return conn.execute(
            "SELECT admission, holdNote FROM projects WHERE id = ?",
            (project,)).fetchone()
    finally:
        conn.close()


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
