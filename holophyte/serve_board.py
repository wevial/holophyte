"""The host daemon's native board writes (KO-758, KO-763): the gate every
one passes, `PUT /projects/NAME/tickets/ID`, the revision-checked edit,
and `POST /projects/NAME/tickets`, `.../tickets/ID/move` and
`.../tickets/ID/cancel`, which file, move and cancel a ticket.

A board write is a stronger power than a read, so the gate is its own:
exactly the machine token (`HostServer.write_token`) on every bind,
loopback included -- a project's own token, accepted for reads, never
carries it -- else 401; host `[serve] actions` on and the project's
`[board] kind = "native"`, else 404, as an unknown route; `If-Match`
naming the revision the edit was read at, a non-negative integer, else
428 (filing has no `If-Match`: a new ticket has no revision); a JSON
object body, else 400. The author is recorded as `console`, never taken
from the body.

The edit is `store.board.edit_ticket()`: 200 with the new revision, 409
with `current` when the ticket has moved past `If-Match`, 422 with the
blocking problems and nothing written; a file, move or cancel answers
the same way through `file_ticket()`, `move_ticket()` and
`cancel_ticket()`, a filing 201. A store failure is the project's 503 or
500, as an action's is. A project daemon has none of these routes.
"""
from __future__ import annotations

from contextlib import closing
from urllib.parse import unquote

import store
import store.board
import store.tickets
from holophyte.config_tables import board_config, board_mode
from holophyte.runs import open_store
from holophyte.serve import authorized
from holophyte.serve_runs import no_store

TICKETS = "/tickets"
TICKETS_PREFIX = TICKETS + "/"
# The columns a console filing or move puts a ticket in.
COLUMNS = ("ready", "backlog")
# What the board records as a console write's author.
AUTHOR = "console"
# Linear's priorities, 0 for none, which a native ticket keeps.
PRIORITIES = range(5)


def ticket_path(path):
    """The identifier `path` edits, `/tickets/ID`; None for any other."""
    if not path.startswith(TICKETS_PREFIX):
        return None
    identifier = unquote(path[len(TICKETS_PREFIX):])
    if not identifier or "/" in identifier:
        return None
    return identifier


def post_path(path):
    """`(verb, identifier)` for a `POST` board path: `("file", None)` for
    `/tickets`, `("move", ID)` or `("cancel", ID)` for `/tickets/ID/VERB`;
    None for any other."""
    if path == TICKETS:
        return "file", None
    identifier, _, verb = path.rpartition("/")
    if verb not in ("move", "cancel"):
        return None
    identifier = ticket_path(identifier)
    return None if identifier is None else (verb, identifier)


def revision_read(header):
    """`If-Match` as the revision it names; None when it names none."""
    text = (header or "").strip()
    if not (text.isascii() and text.isdecimal()):
        return None
    return int(text)


def edit_fields(body):
    """The edit's `(text, priority, labels)` from the request body;
    ValueError naming the first field that is not what it must be."""
    text = body.get("body")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("body must carry the ticket's text as `body`")
    priority = body.get("priority")
    if priority is not None and (isinstance(priority, bool)
                                 or priority not in PRIORITIES):
        raise ValueError("priority must be an integer from 0 to 4")
    labels = body.get("labels")
    if labels is not None and not (
            isinstance(labels, list)
            and all(isinstance(label, str) for label in labels)):
        raise ValueError("labels must be a list of strings")
    return text, priority, labels


def file_fields(body):
    """The filing's `(text, column, priority)` from the request body;
    ValueError naming the first field that is not what it must be."""
    text, priority, _ = edit_fields({**body, "labels": None})
    return text, column_field(body.get("column", "ready")), priority


def move_fields(body):
    """The move's `(column, note)` from the request body; ValueError."""
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string")
    return column_field(body.get("column")), note or None


def cancel_fields(body):
    """The cancel's `(note,)` from the request body; ValueError."""
    note = body.get("note")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("a cancel must carry the reason as `note`")
    return (note,)


def column_field(column):
    if column not in COLUMNS:
        raise ValueError("column must be ready or backlog")
    return column


def gate(handler, scope, path):
    """None when the write may go on, else the `(status, body)` it gets.
    Nothing here opens a store: a route that does not exist is 404 however
    its project's store reads."""
    server = handler.server
    if server.write_token is None or not authorized(
            handler.headers.get("Authorization"), server.write_token):
        return 401, {}
    if scope.project is None:
        return handler.refused(scope)
    if not server.actions or board_mode(scope.project).kind != "native":
        return 404, {"error": "not found", "path": scope.prefix + path}
    return None


def put_ticket(handler, scope, path, identifier):
    """`PUT /tickets/ID` under a project prefix: the gate, then the edit."""
    write(handler, scope, path, edit_fields,
          lambda project, expected, *fields: edit(
              project, identifier, expected, *fields))


def post_ticket(handler, scope, path, verb, identifier):
    """`POST /tickets`, `/tickets/ID/move` or `/tickets/ID/cancel` under a
    project prefix: the gate, then the filing, move or cancel."""
    if verb == "file":
        return write(handler, scope, path, file_fields,
                     lambda project, _, *fields: new_ticket(project, *fields),
                     revisioned=False)
    parse, act = {"move": (move_fields, move),
                  "cancel": (cancel_fields, cancel)}[verb]
    write(handler, scope, path, parse,
          lambda project, expected, *fields: act(
              project, identifier, expected, *fields))


def write(handler, scope, path, parse, act, revisioned=True):
    """One board write in the gate's order: the gate, `If-Match` when
    `revisioned`, the body through `parse`, the scope's store check, then
    `act(project, expected, *fields)`, whose `(status, body)` is answered."""
    try:
        answer = gate(handler, scope, path)
        if answer is not None:
            return handler.answer(*answer)
    except (Exception, SystemExit) as bad:
        return handler.answer(*handler.failure(scope, path, bad))
    expected = revision_read(handler.headers.get("If-Match"))
    if revisioned and expected is None:
        return handler.answer(428, {
            "error": "If-Match must name the revision the edit was read at"})
    try:
        fields = parse(handler.read_body())
    except ValueError as bad:
        return handler.answer(400, {"error": str(bad)})
    try:
        refused = handler.refused(scope)
        if refused is not None:
            return handler.answer(*refused)
        handler.answer(*act(scope.project, expected, *fields))
    except (Exception, SystemExit) as bad:
        handler.answer(*handler.failure(scope, path, bad))


def on_store(project, act):
    """`act(conn, project_id)` on `project`'s store, its `(status, body)`;
    a moved revision is 409 with `current`, a refusal 422 with `problems`."""
    if not project.store_path.exists():
        return 503, no_store(project)
    team = board_config(project).team
    with closing(open_store(project)) as conn:
        project_id = store.tickets.ensure_project(conn, team, project.path)
        try:
            return act(conn, project_id)
        except store.RevisionMoved as moved:
            return 409, {"error": str(moved), "current": moved.current}
        except store.board.FilingRefused as refused:
            return 422, {"problems": refused.problems}


def edit(project, identifier, expected, text, priority, labels):
    """`edit_ticket()` on `project`'s store as the console: `(status, body)`."""
    def act(conn, project_id):
        revision = store.board.edit_ticket(
            conn, project_id, identifier, text, expected, author=AUTHOR,
            priority=priority, labels=labels)
        return 200, {"ticket": identifier, "revision": revision}
    return on_store(project, act)


def new_ticket(project, text, column, priority):
    """`file_ticket()` under the board's `key` as the console: 201 with the
    new ticket at revision 1."""
    key = board_config(project).key

    def act(conn, project_id):
        identifier = store.board.file_ticket(
            conn, project_id, key, text, column=column, priority=priority,
            author=AUTHOR)
        return 201, {"ticket": identifier, "revision": 1}
    return on_store(project, act)


def move(project, identifier, expected, column, note):
    """`move_ticket()` as the console: 200 with the new revision."""
    def act(conn, project_id):
        revision = store.board.move_ticket(
            conn, project_id, identifier, column, expected, author=AUTHOR,
            note=note)
        return 200, {"ticket": identifier, "revision": revision}
    return on_store(project, act)


def cancel(project, identifier, expected, note):
    """`cancel_ticket()` as the console: 200 with the new revision and
    `run`, the live run the cancel asked to abort, None when none. The run
    is read in the cancel's own transaction, so it is the one aborted."""
    def act(conn, project_id):
        with store.transaction(conn):
            row = conn.execute(
                "SELECT activeRunId FROM tickets WHERE projectId = ?"
                " AND linearIdentifier = ?", (project_id, identifier)
            ).fetchone()
            revision = store.board.cancel_ticket(
                conn, project_id, identifier, expected, note, author=AUTHOR)
        return 200, {"ticket": identifier, "revision": revision,
                     "run": row[0] if row else None}
    return on_store(project, act)
