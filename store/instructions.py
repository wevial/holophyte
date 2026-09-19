"""Persist the reply on the most recent recorded instruction for a thread."""
import json

from store import _transaction


def record_instruction_reply(conn, run_id, url, outcome, reply):
    if conn is None or run_id is None:
        return
    with _transaction(conn):
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
