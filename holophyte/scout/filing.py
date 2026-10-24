import json

import store.board
import ticket_template
from holophyte.admission import project_row
from holophyte.loop.runs import open_store
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_runs import no_store
from provider import board_for
from store.enums import ScoutPhase
from store.scouts import ScoutRefused, file_scout_action, scout

SCOUT_FILE_ACTION = "scout-file"
BACKLOG = "Backlog"
DONE = ScoutPhase.DONE.value


class Answered(Exception):
    def __init__(self, code, reply):
        super().__init__(reply.get("detail") or reply.get("error"))
        self.answer = code, reply


def refusal(body):
    for field in ("scout", "item"):
        value = body.get(field)
        if type(value) is not int or value < 1:
            return f"{field} must be a positive integer"
    for field in ("note", "author"):
        if body.get(field) is not None and not isinstance(body[field], str):
            return f"{field} must be text"
    return None


def action_item(conn, scout_id, position):
    row = conn.execute("SELECT item, ticketKey FROM scoutActions"
                       " WHERE scoutId = ? AND position = ?",
                       (scout_id, position)).fetchone()
    return None if row is None else (json.loads(row[0]), row[1])


def judged(project, scout_id, position):
    named = {"scout": scout_id, "item": position}
    conn = open_store(project)
    try:
        row = scout(conn, scout_id)
        if row is None:
            raise Answered(404, {"error": "no such scout", **named,
                                 "detail": f"no scout {scout_id}"})
        if row.phase != DONE:
            raise Answered(200, {"ok": False, **named,
                                 "detail": f"scout {scout_id} is {row.phase};"
                                           " only a done scout's items file"})
        found = action_item(conn, scout_id, position)
        if found is None:
            raise Answered(404, {"error": "no such action item", **named,
                                 "detail": f"scout {scout_id} has no item"
                                           f" {position}"})
        item, filed = found
        if filed is not None:
            raise Answered(200, {"ok": False, **named, "ticket": filed,
                                 "detail": f"scout {scout_id} item {position}"
                                           f" is already filed as {filed}"})
        board = board_for(project)
        if board is None:
            raise Answered(200, {"ok": False, **named,
                                 "detail": "the project has no [board], so"
                                           " nothing is filed"})
        text = item.get("body") if isinstance(item, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise Answered(200, {"ok": False, **named,
                                 "detail": f"scout {scout_id} item {position}"
                                           " has no ticket body"})
        return board, text, store.board.check_ticket(
            conn, project_row(conn, project), text, column="backlog")
    finally:
        conn.close()


def recorded(project, scout_id, position, key):
    conn = open_store(project)
    try:
        file_scout_action(conn, scout_id, position, key)
    except ScoutRefused as refused:
        return f"; the item does not record it: {refused}"
    finally:
        conn.close()
    return ""


def noted(board, key, body):
    note = body.get("note")
    if note is None or not note.strip():
        return ""
    try:
        board.comment(key, f"{console_author(body)}: {note.strip()}")
    except Exception as unposted:
        return f"; its note was not posted: {unposted}"
    return ""


def file_action(project, body):
    refused = refusal(body)
    if refused is not None:
        return 400, {"error": refused}
    if not project.store_path.exists():
        return 503, no_store(project)
    scout_id, position = body["scout"], body["item"]
    named = {"scout": scout_id, "item": position}
    try:
        board, text, judgement = judged(project, scout_id, position)
    except Answered as answered:
        return answered.answer
    problems, advisories = judgement["problems"], judgement["advisories"]
    if problems:
        return 200, {"ok": False, **named, "problems": list(problems),
                     "advisories": list(advisories), "detail": problems[0]}
    ticket = ticket_template.parse(text)
    try:
        key = board.file(ticket.title, text, ticket.estimate_min, BACKLOG,
                         blockers=ticket.depends_on or [])
    except store.board.FilingRefused as refused:
        return 200, {"ok": False, **named, "problems": list(refused.problems),
                     "advisories": list(advisories),
                     "detail": refused.problems[0]}
    missed = recorded(project, scout_id, position, key)
    detail = f"scout {scout_id} item {position} filed as {key} in {BACKLOG}"
    return 200, {"ok": not missed, **named, "ticket": key,
                 "advisories": list(advisories),
                 "detail": detail + missed + noted(board, key, body)}
