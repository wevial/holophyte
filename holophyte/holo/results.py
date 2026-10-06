"""A `holo` write verb's one result: a JSON object, or a line for a person."""
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from holophyte.admission import project_of
from holophyte.config.project import Project, legacy_state_layouts
from holophyte.holo.grammar import (
    ALIASES,
    COMMANDS,
    arguments,
    canonical,
    factory_argv,
)
from store.read import open_readonly

PREFIX = "[holo2] "


class Tee(StringIO):
    def __init__(self, through):
        super().__init__()
        self.through = through

    def write(self, text):
        self.through.write(text)
        return super().write(text)


def run_write(args, resolve):
    out, err = StringIO(), Tee(sys.stderr)
    target, before = [], None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            target = resolve(args.project)
            before = newest_intervention(target)
            code, message = exit_parts(call(args, target))
    except SystemExit as stop:
        code, message = exit_parts(stop.code)
    lines = [line.removeprefix(PREFIX) for line in out.getvalue().splitlines()
             if line.strip()]
    lines += [message.removeprefix(PREFIX)] if message else []
    from_stderr = not lines and code != 0
    if from_stderr:
        lines = err.getvalue().strip().splitlines()[-1:]
    row = recorded_row(args, target, before) if code == 0 else (None, None)
    result = build_result(args, code, lines, row)
    if args.json:
        print(json.dumps(result))
    elif result["ok"]:
        print(human_line(result, lines))
    elif not from_stderr:
        print(human_line(result, lines), file=sys.stderr)
    return code


def usage_result(argv, line):
    options = argv[:argv.index("--")] if "--" in argv else argv
    rows = [(command.words, command) for command in COMMANDS]
    rows += [(alias, canonical(words)) for alias, words in ALIASES]
    command = next((command for prefix, command in rows
                    if tuple(argv[:len(prefix)]) == prefix), None)
    if command is not None and command.records is not None and "--json" in options:
        print(json.dumps({"action": " ".join(command.words), "ok": False,
                          "detail": line, "recorded": None}))


def exit_parts(code):
    if code is None or isinstance(code, int):
        return code or 0, None
    return 1, str(code)


def call(args, target):
    if args.command.mode is None:
        return send_back(args, target)
    from holophyte.cli.entry import _legacy_cli
    return _legacy_cli(target + factory_argv(args))


def send_back(args, target):
    values, note = arguments(args, args.command)
    if not target:
        args.leaf.error("send-back needs its project: -p NAME|PATH")
    if note is None:
        args.leaf.error("send-back records the instruction the run goes back with")
    if not values[0].isdecimal():
        args.leaf.error(f"RUN is a run id, not {values[0]!r}")
    from holophyte.cli.operator import send_back_run
    from holophyte.config.checks import check_config
    project = Project.locate(target[0])
    project.config()
    check_config(project)
    return send_back_run(project, int(values[0]), note)


def store_path(target):
    if not target:
        return None
    path = Project.locate(target[0], adopt=False).store_path
    return path if path.exists() else None


def newest_intervention(target):
    if not target:
        return 0
    paths = [Project.locate(target[0], adopt=False).store_path]
    paths += [source for _, moves in legacy_state_layouts(target[0])
              for source, name in moves if name == "store.db"]
    return max((newest_in(path) for path in paths if path.exists()), default=0)


def newest_in(path):
    conn = open_readonly(path)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                        " AND name = 'interventions'").fetchone() is None:
            return 0
        return conn.execute("SELECT COALESCE(MAX(id), 0)"
                            " FROM interventions").fetchone()[0]
    finally:
        conn.close()


def named(args):
    value = getattr(args, "arg0", None)
    first = args.command.takes[:1]
    if first == ("RUN",):
        return None, int(value) if value.isdecimal() else value
    if first == ("KEY",):
        return value, None
    update = getattr(args, "update", None)
    return (update[-1] if update else None), None


def recorded_row(args, target, before):
    path = store_path(target)
    if before is None or path is None or not args.command.records:
        return None, None
    ticket, run = named(args)
    actions = args.command.records
    conn = open_readonly(path)
    try:
        project = project_of(conn, Project.locate(target[0], adopt=False))
        row = conn.execute(
            "SELECT i.id, i.runId FROM interventions i"
            " LEFT JOIN runs r ON r.id = i.runId"
            " LEFT JOIN tickets t ON t.id = r.ticketId"
            f" WHERE i.id > ? AND i.action IN ({','.join('?' * len(actions))})"
            " AND (t.linearIdentifier = ? OR i.runId = ?"
            " OR (i.runId IS NULL AND i.projectId = ?))"
            " ORDER BY i.id DESC LIMIT 1",
            (before, *actions, ticket, run, project)).fetchone()
    finally:
        conn.close()
    return row or (None, None)


def build_result(args, code, lines, row):
    recorded, recorded_run = row
    ticket, run = named(args)
    result = {"action": " ".join(args.command.words), "ok": code == 0,
              "detail": "\n".join(lines), "recorded": recorded}
    if ticket is not None:
        result["ticket"] = ticket
    if run is not None or recorded_run is not None:
        result["run"] = recorded_run if run is None else run
    return result


def human_line(result, lines):
    if not result["ok"]:
        return "✗ " + " · ".join(lines)
    parts = lines or [result["action"]]
    if result["recorded"] is not None:
        parts = [*parts, f"intervention {result['recorded']} recorded"]
    return "✓ " + " · ".join(parts)
