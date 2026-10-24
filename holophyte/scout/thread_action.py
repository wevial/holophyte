from holophyte.loop.runs import open_store
from holophyte.scout.filing import ROW_ID_LIMIT
from holophyte.scout.follow_up import PASS_PHASES, current_pass
from holophyte.scout.launch import start
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_runs import no_store
from store.scout_thread import ASK, STEER, ThreadRefused, post_message
from store.scouts import ANSWERING, DONE, scout

SCOUT_MESSAGE_ACTION = "scout-message"
MODES = (ASK, STEER)


def refusal(body):
    value = body.get("scout")
    if type(value) is not int or value < 1:
        return "scout must be a positive integer"
    if body.get("mode") not in MODES:
        return f"mode must be one of {', '.join(MODES)}"
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return "text must be non-blank text"
    answers = body.get("answers")
    if answers is not None and (type(answers) is not int or answers < 1):
        return "answers must be a positive integer: the card's message id"
    return None


def refusal_reason(row, refused, answers):
    if row.phase != DONE:
        return "not_done"
    if refused.subject == f"message {answers}":
        return "card_refused"
    return "ask_waiting"


def record(project, scout_id, mode, text, author, answers=None):
    conn = open_store(project)
    try:
        row = scout(conn, scout_id) if scout_id < ROW_ID_LIMIT else None
        if row is None:
            return 404, {"error": "no such scout", "scout": scout_id,
                         "detail": f"no scout {scout_id}"}
        try:
            message = post_message(conn, scout_id, author, mode, text,
                                   answers_id=answers)
        except ThreadRefused as refused:
            return 200, {"ok": False, "scout": scout_id,
                         "reason": refusal_reason(row, refused, answers),
                         "detail": str(refused)}
    finally:
        conn.close()
    return 200, message


def taken(project, scout_id, message):
    conn = open_store(project)
    try:
        (consumed_by,) = conn.execute(
            "SELECT consumedBy FROM threadMessages WHERE id = ?",
            (message,)).fetchone()
        steered = current_pass(conn, scout_id) or {}
        number = (steered.get("pass") if steered.get("message") == message
                  else None)
        return consumed_by is not None, scout(conn, scout_id).phase, number
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
                           console_author(body), body.get("answers"))
    if isinstance(message, dict):
        return code, message
    _, said = start(project, scout_id)
    consumed, phase, number = taken(project, scout_id, message)
    if not consumed:
        detail = f"message {message} waits for a scout slot: {said}"
    elif phase == ANSWERING:
        detail = f"scout {scout_id} is answering message {message}"
    elif phase in PASS_PHASES and number:
        detail = f"scout {scout_id} runs pass {number} for message {message}"
    else:
        work = "answer" if body["mode"] == ASK else "pass"
        detail = f"message {message} was taken but its {work} ended: {said}"
    return 200, {"ok": True, "scout": scout_id, "message": message,
                 "queued": not consumed, "recorded": None, "detail": detail}
