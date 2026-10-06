"""`holo start` and `holo stop`: a loop unit started, or its admission held."""
import store
import store.read
from holophyte.admission import held_line, project_of, project_row, set_hold, state
from holophyte.config.project import Project
from holophyte.holo.grammar import arguments
from holophyte.host.registry import Host, HostError, registered_at
from holophyte.loop.runs import open_store


def checked(project):
    from holophyte.config.checks import check_config
    project.config()
    check_config(project)
    return project


def unit_name(project, host=None):
    host = Host.locate() if host is None else host
    try:
        entry = registered_at(host, project)
    except HostError as bad:
        raise SystemExit(str(bad)) from None
    if entry is None:
        raise SystemExit(f"[holo2] {host.path} does not register {project.path};"
                         " its loop unit has no instance name")
    if entry.name is None:
        raise SystemExit(f"[holo2] the {host.path} entry {entry.path} has no"
                         f" [serve] name ({entry.error}); its loop unit has no"
                         " instance name")
    return entry.name


def stored(project):
    conn = open_store(project)
    try:
        project_row(conn, project)
        return state(conn, project)
    except ValueError as refused:
        raise SystemExit(f"[holo2] {refused}") from None
    finally:
        conn.close()


def release(project, note):
    conn = open_store(project)
    try:
        set_hold(conn, project, False, note)
    except ValueError as refused:
        raise SystemExit(f"[holo2] {refused}") from None
    finally:
        conn.close()
    print(f"[holo2] hold released: {note}")


def ready_count(project):
    conn = store.read.open_readonly(project.store_path)
    try:
        key = project_of(conn, project)
        return 0 if key is None else len(store.read.ready_tickets(conn, key))
    finally:
        conn.close()


def given_note(args, verb):
    _, note = arguments(args, args.command)
    if note is not None and not note.strip():
        args.leaf.error(f"{verb}'s note must say why (non-blank text)")
    return note


def start(args, target):
    from holophyte.serve.serve_actions import unit_action
    note = given_note(args, "start")
    project = Project.locate(target[0])
    name = unit_name(project)
    admitted, hold_note = stored(checked(project))
    if admitted == "disabled":
        raise SystemExit(f"[holo2] project {project.path} disabled: {hold_note};"
                         " holo start does not enable it")
    if admitted == "held" and note is None:
        raise SystemExit(f"[holo2] project {project.path} held: {hold_note};"
                         " holo start NOTE releases the hold and starts its loop")
    if admitted == "held":
        release(project, note)
    asked = "holo start" + (f": {note}" if note is not None else "")
    _, result = unit_action(project, "launch-loop", name, ("holo", asked))
    if not result["ok"]:
        raise SystemExit(f"[holo2] {result['detail']}")
    count = ready_count(project)
    print(f"[holo2] started {result['unit']};"
          f" {count} {'ticket' if count == 1 else 'tickets'} ready")


def foreground(args, target):
    _, note = arguments(args, args.command)
    if note is not None or args.json:
        args.leaf.error("--foreground runs the loop in this terminal: it takes"
                        " no note and prints no result")
    from holophyte.cli.entry import _legacy_cli
    return _legacy_cli(target)


def live_runs(conn, project_id):
    parked = sorted(store.PARKED_PHASES)
    return conn.execute(
        "SELECT r.id, t.linearIdentifier FROM runs r"
        " JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.projectId = ? AND r.endedAt IS NULL"
        f" AND r.phase NOT IN ({','.join('?' * len(parked))}) ORDER BY r.id",
        (project_id, *parked)).fetchall()


def hold(conn, project, note):
    try:
        key = set_hold(conn, project, True, note)
    except ValueError as refused:
        raise SystemExit(f"[holo2] {refused}") from None
    print(held_line(conn, key))
    return key


def abort_each(project, conn, runs, note, board):
    from holophyte.loop.stop import abort_run
    refused = False
    for run_id, ticket in runs:
        try:
            ended = abort_run(project, conn, run_id, note, provider=board)
        except ValueError as error:
            print(f"[holo2] run {run_id} ({ticket}) not aborted: {error}")
            refused = True
            continue
        print(f"[holo2] aborted run {run_id} ({ticket}): " + (
            "no live worker; ended abandoned and parked" if ended
            else "it stops at its next safe point"))
    return refused


def stop(args, target):
    note = given_note(args, "stop")
    if note is None:
        args.leaf.error("stop holds the project: give a note saying why")
    from holophyte.cli.board_verbs import require_board
    from provider import board_for
    project = checked(Project.locate(target[0]))
    board = require_board(project, board_for(project)) if args.now else None
    conn = open_store(project)
    try:
        runs = live_runs(conn, hold(conn, project, note))
        named = ", ".join(f"run {run_id} ({ticket})" for run_id, ticket in runs)
        if not runs:
            print("[holo2] no run is live: the loop exits at its next idle check")
        elif not args.now:
            print(f"[holo2] the loop exits after its live runs finish: {named}")
        elif abort_each(project, conn, runs, note, board):
            return 1
        else:
            print("[holo2] the loop exits once the aborted runs have stopped")
    finally:
        conn.close()
    return 0


VERBS = {("start",): start, ("stop",): stop}
