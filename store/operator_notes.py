"""Private maintainer instructions attached to a candidate's PR lineage."""
import json

import store
from store.operate import _release_parked
from store.schema import _transaction


def send_back(conn, run_id, note, author):
    """Record before releasing, atomically, refusing stale cards and live runs."""
    if not isinstance(note, str) or not note.strip():
        raise ValueError("note must be non-blank text")
    if not isinstance(author, str) or not author.strip():
        raise ValueError("author must be non-blank text")
    data = {"note": note.strip(), "author": author.strip()}
    with _transaction(conn):
        row = conn.execute(
            "SELECT t.id FROM tickets t JOIN runs r ON r.ticketId = t.id"
            " WHERE r.id = ? AND t.lastRunId = r.id", (run_id,)).fetchone()
        if row is None:
            raise ValueError("run must be the ticket's latest parked attempt")
        def record_note():
            store.record_event(conn, run_id, "operator_note",
                               f"{data['author']}: {data['note']}",
                               level="detail", payload=json.dumps(data))
        _release_parked(conn, row[0], "operator_note", json.dumps(data),
                        "sent back with a maintainer instruction", None,
                        require_pr=True, guidance=json.dumps(data),
                        before_release=record_note)
        return conn.execute("SELECT id FROM runEvents WHERE runId = ?"
                            " AND kind = 'operator_note' ORDER BY id DESC LIMIT 1",
                            (run_id,)).fetchone()[0]


def notes(conn, run_id, pending=False, pr_url=None):
    """Read instructions for this candidate, including prior released attempts.

    Consumption is an append-only event referencing the original event id.
    The source instruction stays immutable; retries retain the amended contract.
    """
    if conn is None or run_id is None:
        return []
    rows = conn.execute(
        "SELECT e.id, e.payload FROM runEvents e JOIN runs r ON r.id = e.runId"
        " JOIN runs current ON current.id = ? WHERE e.kind = 'operator_note'"
        " AND r.ticketId = current.ticketId AND r.id <= current.id"
        " AND (r.id = current.id OR r.prUrl = COALESCE(current.prUrl, ?)"
        " OR EXISTS (SELECT 1 FROM runEvents c WHERE c.runId = current.id"
        " AND c.kind = 'operator_note_consumed'"
        " AND json_extract(c.payload, '$.event_id') = e.id)) ORDER BY e.id",
        (run_id, pr_url)).fetchall()
    result = []
    for event_id, payload in rows:
        data = json.loads(payload)
        consumed = conn.execute(
            "SELECT runId, payload FROM runEvents WHERE kind = 'operator_note_consumed'"
            " AND json_extract(payload, '$.event_id') = ? ORDER BY id LIMIT 1",
            (event_id,)).fetchone()
        data.update(kind="operator_note", event_id=event_id, consumed=bool(consumed))
        if consumed:
            data.update(run_id=consumed[0], round=json.loads(consumed[1])["round"])
        if not pending or not consumed:
            result.append(data)
    return result


def consume(conn, run_id, event_ids, rnd):
    """Mark the instructions consumed as their fix starts; retain round evidence."""
    with _transaction(conn):
        for event_id in event_ids:
            store.record_event(conn, run_id, "operator_note_consumed",
                               f"operator_note event {event_id} drove round {rnd}",
                               level="detail", payload=json.dumps(
                                   {"event_id": event_id, "round": rnd}))
            store.record_ledger(conn, run_id, "round",
                                f"Round {rnd}: ADDRESS operator_note event {event_id}")


def round_notes(conn, run_id, rnd):
    return [n for n in notes(conn, run_id)
            if n.get("run_id") == run_id and n.get("round") == rnd]


def report_lines(conn):
    rows = conn.execute("SELECT runId, payload FROM runEvents"
                        " WHERE kind = 'operator_note_consumed' ORDER BY id")
    lines = []
    for run_id, payload in rows:
        data = json.loads(payload)
        note = conn.execute("SELECT payload FROM runEvents WHERE id = ?",
                            (data["event_id"],)).fetchone()
        instruction = json.loads(note[0])
        # Escape control characters so private text cannot create report rows.
        author = repr(instruction["author"])[1:-1]
        text = repr(instruction["note"])[1:-1]
        lines.append(f"Run {run_id} round {data['round']}: operator_note event "
                     f"{data['event_id']} by {author}: {text}")
    return lines
