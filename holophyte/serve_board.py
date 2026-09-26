"""The host daemon's native board writes (KO-758): the gate every one
passes and `PUT /projects/NAME/tickets/ID`, the revision-checked edit.

A board write is a stronger power than a read, so the gate is its own:
exactly the machine token (`HostServer.write_token`) on every bind,
loopback included -- a project's own token, accepted for reads, never
carries it -- else 401; host `[serve] actions` on and the project's
`[board] kind = "native"`, else 404, as an unknown route; `If-Match`
naming the revision the edit was read at, a non-negative integer, else
428; a JSON object body, else 400. The author is recorded as `console`,
never taken from the body.

The edit is `store.board.edit_ticket()`: 200 with the new revision, 409
with `current` when the ticket has moved past `If-Match`, 422 with the
blocking problems and nothing written. A store failure is the project's
503 or 500, as an action's is. A project daemon has none of these routes.
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

TICKETS_PREFIX = "/tickets/"
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
    try:
        answer = gate(handler, scope, path)
        if answer is not None:
            return handler.answer(*answer)
    except (Exception, SystemExit) as bad:
        return handler.answer(*handler.failure(scope, path, bad))
    expected = revision_read(handler.headers.get("If-Match"))
    if expected is None:
        return handler.answer(428, {
            "error": "If-Match must name the revision the edit was read at"})
    try:
        fields = edit_fields(handler.read_body())
    except ValueError as bad:
        return handler.answer(400, {"error": str(bad)})
    try:
        refused = handler.refused(scope)
        if refused is not None:
            return handler.answer(*refused)
        handler.answer(*edit(scope.project, identifier, expected, *fields))
    except (Exception, SystemExit) as bad:
        handler.answer(*handler.failure(scope, path, bad))


def edit(project, identifier, expected, text, priority, labels):
    """`edit_ticket()` on `project`'s store as the console: `(status, body)`."""
    if not project.store_path.exists():
        return 503, no_store(project)
    team = board_config(project).team
    with closing(open_store(project)) as conn:
        project_id = store.tickets.ensure_project(conn, team, project.path)
        try:
            revision = store.board.edit_ticket(
                conn, project_id, identifier, text, expected, author=AUTHOR,
                priority=priority, labels=labels)
        except store.RevisionMoved as moved:
            return 409, {"error": str(moved), "current": moved.current}
        except store.board.FilingRefused as refused:
            return 422, {"problems": refused.problems}
    return 200, {"ticket": identifier, "revision": revision}
