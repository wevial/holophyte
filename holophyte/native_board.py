"""`NativeBoard`: a `[board] kind = "native"` project's board, the store.

A native board has no second copy to sync, read back or post notes to
(KO-754): its reads answer from the project's store, its lease and state
writes do nothing -- a claim's lease read-back then stands -- and
`comment()` only records the note. Filing, editing and the body read-back
go through `store.board`, so `--file-ticket` reaches the store through the
same `file()`, `update()` and `stored_body()` it drives on Linear. Nothing
here imports or asks Linear.

The store is opened per call, read-only for a read; a read of a store not
created yet answers as a board with no tickets.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import store
import store.board
import store.tickets
from provider import GONE, parse_body
from store.read import claimable, open_readonly

# `--file-ticket`'s workflow state names as a native ticket's column.
STATE_COLUMNS = {"Todo": "ready", "Backlog": "backlog"}
_COLUMNS = ("id, linearIssueId, linearIdentifier, title, body, timeBoxMs,"
            " priority, labels, url, boardState, boardUpdatedAt, filedAt,"
            " boardColumn, status, revision")


class NativeBoard:
    """The `Board` members answered from `project`'s store; see the module
    docstring. `team` is the store's project key, `key` the `KEY` of the
    board's `KEY-n` identifiers."""

    store_mode = True
    native = True

    def __init__(self, project, key, team):
        self.project = project
        self.key = key
        self.team = team
        self.last_listing = None

    # --- store access --------------------------------------------------------

    def _write(self):
        """A writable connection and this board's project id."""
        from holophyte.runs import open_store
        conn = open_store(self.project)
        project_id = store.tickets.ensure_project(conn, self.team,
                                                  self.project.path)
        return conn, project_id

    def _rows(self, where, params):
        """This project's ticket rows matching `where`; [] without a store
        or a project row."""
        if not Path(self.project.store_path).exists():
            return []
        with closing(open_readonly(self.project.store_path)) as conn:
            return conn.execute(
                f"SELECT {_COLUMNS} FROM tickets WHERE projectId = (SELECT id"
                f" FROM projects WHERE linearTeamId = ?) AND ({where})",
                (self.team, *params)).fetchall()

    def _row(self, issue_id):
        rows = self._rows("linearIssueId = ? OR linearIdentifier = ?",
                          (issue_id, issue_id))
        return rows[0] if rows else None

    def _claimable_ids(self):
        if not Path(self.project.store_path).exists():
            return []
        with closing(open_readonly(self.project.store_path)) as conn:
            row = conn.execute("SELECT id FROM projects WHERE linearTeamId = ?",
                               (self.team,)).fetchone()
            return [] if row is None else [t.id for t in claimable(conn, row[0])]

    # --- reads ---------------------------------------------------------------

    def fetch_task(self, issue_id):
        row = self._row(issue_id)
        return None if row is None else _task(row)

    def states(self, identifiers):
        rows = {row[2]: row for row in self._rows(
            "linearIdentifier IN (SELECT value FROM json_each(?))",
            (_json(identifiers),))}
        answer = {}
        for identifier in identifiers:
            row = rows.get(identifier)
            if row is None:
                answer[identifier] = {"state": GONE, "name": None,
                                      "column": None}
                continue
            state_name, column, status = row[9], row[12], row[13]
            if status == "merged":
                answer[identifier] = {"state": "completed", "name": state_name,
                                      "column": None}
            elif column == "canceled":
                answer[identifier] = {"state": "canceled", "name": state_name,
                                      "column": "canceled"}
            else:
                answer[identifier] = {"state": "open", "name": state_name,
                                      "column": column}
        return answer

    def closed_identifiers(self, identifiers):
        return {identifier: state["state"]
                for identifier, state in self.states(identifiers).items()
                if state["state"] in ("completed", "canceled")}

    def ready_issues(self):
        ids = self._claimable_ids()
        rows = {row[0]: row for row in self._rows(
            "id IN (SELECT value FROM json_each(?))", (_json(ids),))}
        return [dict(_task(rows[i]), blocked_by=[]) for i in ids if i in rows]

    def listing(self):
        return self.ready_issues()

    def claim_next(self, skip=(), order="identifier"):
        raise RuntimeError("a native board's queue is the store's: claim from"
                           " the store, not the board")

    # --- writes --------------------------------------------------------------

    def set_state(self, issue_id, state_name):
        pass

    def label_issue(self, issue_id, name):
        pass

    def unlabel_issue(self, issue_id, name):
        pass

    def issue_labels(self, issue_id):
        return []

    def comment(self, task_id, body):
        row = self._row(task_id)
        if row is None:
            raise RuntimeError(f"no ticket {task_id!r} on native board {self.key}")
        digest = hashlib.sha256(body.encode()).hexdigest()
        conn, _ = self._write()
        with closing(conn):
            store.record_note(conn, row[0], "comment", body, f"comment:{digest}")

    def file(self, title, body, estimate, state, priority=None, blockers=()):
        """File `body` in the column `state` names; its title, estimate and
        blockers are the body's own, as the store reads them from it."""
        if state not in STATE_COLUMNS:
            raise RuntimeError(f"a native board files into Todo or Backlog,"
                               f" not {state!r}")
        conn, project_id = self._write()
        with closing(conn):
            return store.board.file_ticket(
                conn, project_id, self.key, body, column=STATE_COLUMNS[state],
                priority=priority, author="cli")

    def update(self, identifier, title, body, estimate, blockers=(), *,
               revision=None, priority=None, labels=None):
        """Replace the body at `revision`, the one the editor read; the
        store resolves the body's `Depends on:`, so nothing is added or
        kept beside it."""
        if revision is None:
            raise RuntimeError(f"updating {identifier} on a native board needs"
                               " --revision, the revision the edit was read at")
        conn, project_id = self._write()
        with closing(conn):
            store.board.edit_ticket(conn, project_id, identifier, body,
                                    revision, author="cli", priority=priority,
                                    labels=labels)
        return [], []

    def stored_body(self, identifier):
        row = self._row(identifier)
        if row is None:
            raise RuntimeError(f"no ticket {identifier!r} on native board {self.key}")
        return row[4]


def _json(values):
    return json.dumps(list(values))


def _task(row):
    """A ticket row as the task dict: its body parsed as a board parses it,
    with the row's own fields over it and `store_revision` naming the
    revision it was read at, so a mirror of it never reverts a later edit."""
    (_, issue_id, identifier, title, body, time_box_ms, priority, labels, url,
     state_name, updated_at, filed_at, column, status, revision) = row
    task = parse_body(identifier, body)
    task.update(issue_id=issue_id, title=title, priority=priority,
                labels=json.loads(labels), url=url, board_state=state_name,
                updatedAt=updated_at, filed_at=filed_at,
                column=None if status == "merged" else column,
                store_revision=revision)
    if time_box_ms is not None:
        task["budget_min"] = time_box_ms // 60000
    return task
