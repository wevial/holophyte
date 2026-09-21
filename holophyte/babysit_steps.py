"""Narrative PR steps, deduplicated across polling and babysit passes."""
import store

STEPS = frozenset({"checks", "quiet", "threads", "fix", "conflict_merge",
                   "covering_review", "parked"})


def record_step(conn, run_id, step):
    """Record entry before work starts, once per change of step."""
    if step not in STEPS:
        raise ValueError(f"unknown babysit step: {step}")
    if conn is None or run_id is None:
        return
    previous = conn.execute(
        "SELECT summary FROM runEvents WHERE runId = ? AND kind = 'babysit_step'"
        " ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    if previous is None or previous[0] != step:
        store.record_event(conn, run_id, "babysit_step", step)
