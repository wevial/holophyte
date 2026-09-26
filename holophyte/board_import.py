"""`--board-import`: copy every open Linear issue into the store (KO-756).

Stage 6 of the move off Linear: before a project's `[board] kind` is set to
`"native"`, each open issue of its Linear board, Backlog included, is
upserted into the store by board id (the issue UUID), so nothing the store
lacks is lost when nothing reads Linear again. Rows, runs, ledger and
`dependsOn` the store already holds stay as they are. Setting `kind` is the
maintainer's step (the runbook's "Move a project to the native board").
"""
import sys

import store
from holophyte.board import body_problems, mirror_key, mirror_task, on_pull_request


class _DryRun(Exception):
    """Raised out of the import's transaction so a dry run rolls back."""


def board_import(project, board, dry_run=False, out=None):
    """Upsert `board.open_issues()` into `project`'s store; return 0.

    The board is asked first, outside any transaction, and every issue is
    then mirrored in one `store.transaction()` the way the queue mirror
    does: its column from the answer, specced by `body_problems()`. A row
    the store holds keeps its `dependsOn` (`depends_on=None`); a new one
    takes the issue's `blocked_by`. One line per issue says `new`,
    `changed` (its revision moved) or `unchanged`, and a summary counts
    them with the pushes and notes still pending for Linear. A dry run
    raises out of the transaction after the summary, so it rolls back, as
    a failure part-way does; running it again is the restart, the upsert
    being keyed by board id.
    """
    out = out or sys.stdout
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
    """Mirror one open issue; return `new`, `changed` or `unchanged`."""
    before = _revision(conn, project_id, task)
    problems = body_problems(
        task, project.path,
        on_pull_request=on_pull_request(conn, project_id, task))
    # A held row keeps its dependsOn; a new one takes the board's blockers.
    depends_on = task.get("blocked_by", []) if before is None else None
    mirror_task(conn, project_id, task, specced=not problems,
                depends_on=depends_on)
    if before is None:
        return "new"
    after = _revision(conn, project_id, task)
    return "unchanged" if after == before else "changed"


def _revision(conn, project_id, task):
    """The stored row's revision for `task`, or None with no row."""
    row = conn.execute(
        "SELECT revision FROM tickets WHERE linearIssueId = ? AND projectId = ?",
        (mirror_key(task), project_id)).fetchone()
    return None if row is None else row[0]


def _pending(conn, project_id):
    """The project's pushes and notes not yet delivered to Linear."""
    pushes = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE projectId = ?"
        " AND pushState IS NOT NULL", (project_id,)).fetchone()[0]
    notes = conn.execute(
        "SELECT COUNT(*) FROM ticketNotes n JOIN tickets t ON t.id = n.ticketId"
        " WHERE t.projectId = ? AND n.postedAt IS NULL",
        (project_id,)).fetchone()[0]
    return pushes, notes
