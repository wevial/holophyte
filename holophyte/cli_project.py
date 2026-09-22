"""Explicit project registration and admission, against one store at a time."""
import argparse
import subprocess
from pathlib import Path

import store
from holophyte.config import check_config
from holophyte.config_tables import board_config
from holophyte.runs import open_store
from holophyte.target import Target


def project_cli(argv):
    parser = argparse.ArgumentParser(prog="factory.py project")
    commands = parser.add_subparsers(dest="command", required=True)
    for verb in ("add", "list", "enable", "hold", "disable"):
        command = commands.add_parser(verb)
        command.add_argument("--store", type=Path,
                             help="store database (default: target/current repository)")
        if verb == "add":
            command.add_argument("path", type=Path)
        elif verb != "list":
            command.add_argument("name")
            command.add_argument("--note", required=verb != "enable",
                                 default="enabled by operator")
    args = parser.parse_args(argv)
    if hasattr(args, "note") and not args.note.strip():
        parser.error("--note must be non-empty text")
    path = args.path if args.command == "add" else Path.cwd()
    target = Target.locate(path.resolve())
    try:
        settings = _validate(target) if args.command == "add" else None
        conn = open_store(target, args.store)
        try:
            _dispatch(conn, args, target, settings)
        finally:
            conn.close()
    except ValueError as error:
        raise SystemExit(str(error)) from None


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


def _dispatch(conn, args, target, settings):
    if args.command == "add":
        project = store.register_project(conn, settings.team, target.path)
        print(f"project {project} {target.path.name} {target.path} enabled")
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
