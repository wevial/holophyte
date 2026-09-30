"""`--board-import`: upsert every open issue of the board into the store."""
import sys

import store
from holophyte.board.projection import (
    body_problems,
    mirror_key,
    mirror_task,
    on_pull_request,
)


class _DryRun(Exception):
    pass


def board_import(project, board, dry_run=False, out=None):
    out = out or sys.stdout
    # store.open() creates an absent file, so a dry run refuses one first.
    if dry_run and not project.store_path.is_file():
        raise SystemExit(f"[holo2] no store at {project.store_path}")
    issues = board.open_issues()
    conn = store.open(project.store_path, migrate=not dry_run)
    try:
        with store.transaction(conn):
            project_id = store.tickets.ensure_project(conn, board.team,
                                                      project.path)
            counts = {"new": 0, "changed": 0, "unchanged": 0}
            for task in issues:
                verdict = _import_issue(conn, project, project_id, task)
                counts[verdict] += 1
                print(f"[holo2] {task['id']}: {verdict}", file=out)
            pushes, notes = _pending(conn, project_id)
            print(f"[holo2] board import: {counts['new']} new, "
                  f"{counts['changed']} changed, {counts['unchanged']} "
                  f"unchanged; {pushes} pushes and {notes} notes pending "
                  "for Linear", file=out)
            if dry_run:
                raise _DryRun
    except _DryRun:
        print("[holo2] dry run: nothing written", file=out)
    finally:
        conn.close()
    return 0


def _import_issue(conn, project, project_id, task):
    before = _revision(conn, project_id, task)
    problems = body_problems(
        task, project.path,
        on_pull_request=on_pull_request(conn, project_id, task))
    depends_on = task.get("blocked_by", []) if before is None else None
    mirror_task(conn, project_id, task, specced=not problems,
                depends_on=depends_on)
    if before is None:
        return "new"
    after = _revision(conn, project_id, task)
    return "unchanged" if after == before else "changed"


def _revision(conn, project_id, task):
    row = conn.execute(
        "SELECT revision FROM tickets WHERE linearIssueId = ? AND projectId = ?",
        (mirror_key(task), project_id)).fetchone()
    return None if row is None else row[0]


def _pending(conn, project_id):
    pushes = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE projectId = ?"
        " AND pushState IS NOT NULL", (project_id,)).fetchone()[0]
    notes = conn.execute(
        "SELECT COUNT(*) FROM ticketNotes n JOIN tickets t ON t.id = n.ticketId"
        " WHERE t.projectId = ? AND n.postedAt IS NULL",
        (project_id,)).fetchone()[0]
    return pushes, notes
