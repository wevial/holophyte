import re

import store

STEPS = frozenset({"checks", "quiet", "threads", "fix", "conflict_merge",
                   "covering_review", "parked"})


def record_step(conn, run_id, step):
    if step not in STEPS:
        raise ValueError(f"unknown babysit step: {step}")
    if conn is None or run_id is None:
        return
    previous = conn.execute(
        "SELECT summary FROM runEvents WHERE runId = ? AND kind = 'babysit_step'"
        " ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    if previous is None or previous[0] != step:
        store.record_event(conn, run_id, "babysit_step", step)


CHECK_FIX_ROUND = re.compile(r"^check fix round (\d+) of (\d+) on \S+: ")


def running_step(conn, run_id):
    steps = conn.execute(
        "SELECT seq, summary FROM runEvents WHERE runId = ?"
        " AND kind = 'babysit_step' ORDER BY seq DESC", (run_id,)).fetchall()
    if not steps or steps[0][1] != "fix":
        return None
    since = next((seq for seq, step in steps if step != "fix"), -1)
    row = conn.execute(
        "SELECT summary FROM runEvents WHERE runId = ? AND kind = 'check_fix_round'"
        " AND seq > ? ORDER BY seq DESC LIMIT 1", (run_id, since)).fetchone()
    match = row and CHECK_FIX_ROUND.match(row[0])
    return f"fixing checks (round {match[1]} of {match[2]})" if match else None
