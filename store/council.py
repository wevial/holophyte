"""Each reviewer seat's reply in a council review round."""
import json

from holophyte.redact import redact_document, redact_values

from .schema import _transaction


def record_seat(conn, run_id, rnd, seat, route, status, *, started_at,
                ended_at, seconds=None, verdict=None, score=None, reply=None,
                reason=None):
    document = None if reply is None else json.dumps(redact_document(reply),
                                                     sort_keys=True)
    with _transaction(conn):
        return conn.execute(
            "INSERT INTO councilSeats (runId, round, seat, route, status,"
            " verdict, score, reply, reason, seconds, startedAt, endedAt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, rnd, seat, redact_values(route), status, verdict, score,
             document, None if reason is None else redact_values(reason),
             seconds, started_at, ended_at)).lastrowid


def record_consolidator(conn, run_id, rnd, route):
    with _transaction(conn):
        conn.execute(
            "UPDATE reviewRounds SET consolidatedBy = ? WHERE runId = ?"
            " AND round = ?", (redact_values(route), run_id, rnd))
