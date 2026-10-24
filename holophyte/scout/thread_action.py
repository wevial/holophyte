import store
from holophyte.scout.filing import ROW_ID_LIMIT
from holophyte.scout.launch import start
from holophyte.serve.serve_actions import action_store
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_runs import no_store
from store.scout_thread import ASK, ThreadRefused, post_message
from store.scouts import ANSWERING, DONE, scout

SCOUT_MESSAGE_ACTION = "scout-message"
MODES = (ASK,)


def refusal(body):
    value = body.get("scout")
    if type(value) is not int or value < 1:
        return "scout must be a positive integer"
    if body.get("mode") not in MODES:
        return f"mode must be one of {', '.join(MODES)}"
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return "text must be non-blank text"
    return None


def record(project, scout_id, mode, text, author):
    conn = action_store(project)
    try:
        row = scout(conn, scout_id) if scout_id < ROW_ID_LIMIT else None
        if row is None:
            return 404, {"error": "no such scout", "scout": scout_id,
                         "detail": f"no scout {scout_id}"}
        try:
            message = post_message(conn, scout_id, author, mode, text)
        except ThreadRefused as refused:
            reason = "not_done" if row.phase != DONE else "ask_waiting"
            return 200, {"ok": False, "scout": scout_id, "reason": reason,
                         "detail": str(refused)}
    finally:
        conn.close()
    return 200, message


def taken(project, scout_id, message):
    conn = store.open(str(project.store_path))
    try:
        (consumed_by,) = conn.execute(
            "SELECT consumedBy FROM threadMessages WHERE id = ?",
            (message,)).fetchone()
        return consumed_by is not None, scout(conn, scout_id).phase
    finally:
        conn.close()


def message_action(project, body):
    refused = refusal(body)
    if refused is not None:
        return 400, {"error": refused}
    if not project.store_path.exists():
        return 503, no_store(project)
    scout_id = body["scout"]
    code, message = record(project, scout_id, body["mode"], body["text"],
                           console_author(body))
    if code != 200 or isinstance(message, dict):
        return code, message
    ran, said = start(project, scout_id)
    consumed, phase = taken(project, scout_id, message)
    if not consumed:
        detail = f"message {message} waits for a scout slot: {said}"
    elif ran or phase == ANSWERING:
        detail = f"scout {scout_id} is answering message {message}"
    else:
        detail = f"message {message} was taken but its answer ended: {said}"
    return 200, {"ok": True, "scout": scout_id, "message": message,
                 "queued": not consumed, "recorded": None, "detail": detail}
