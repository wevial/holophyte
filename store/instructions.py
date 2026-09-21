"""Persist instruction replies in review findings or standalone answer events."""
import json

from store import _transaction, record_event


def record_instruction_reply(conn, run_id, url, outcome, reply, *, instruction=None):
    """An ask supplies its instruction because answering consumes no review round."""
    if conn is None or run_id is None:
        return
    with _transaction(conn):
        if instruction is not None:
            record_event(conn, run_id, "instruction",
                         json.dumps(dict(instruction, outcome=outcome, reply=reply)))
            return
        rows = conn.execute(
            "SELECT id, findings FROM reviewRounds WHERE runId = ? ORDER BY round DESC",
            (run_id,)).fetchall()
        for row_id, document in rows:
            findings = json.loads(document)
            for finding in findings:
                if finding.get("kind") == "instruction" and finding.get("url") == url:
                    finding.update(outcome=outcome, reply=reply)
                    conn.execute("UPDATE reviewRounds SET findings = ? WHERE id = ?",
                                 (json.dumps(findings), row_id))
                    return
