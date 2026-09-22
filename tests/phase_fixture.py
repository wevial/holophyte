"""Arrange run states for reader/operator tests without illegal phase writes.

These helpers are explicit fixture calls, never patches on the runtime writer.
Live loop tests still use the production state machine without adaptation.
"""
from collections import deque

import store


def advance_phase(conn, run_id, phase, note=None, now=None):
    """Walk live edges to a setup phase, without detouring through failure."""
    previous = store.run_phase(conn, run_id)
    queue = deque([(previous, [])])
    seen = {previous}
    while queue:
        current, path = queue.popleft()
        if current == phase:
            for step in path[:-1]:
                store.set_phase(conn, run_id, step, now=now)
            store.set_phase(conn, run_id, phase, note=note, now=now)
            return previous
        for step in sorted(store.RUN_PHASE_TRANSITIONS[current]):
            if step not in seen and step not in store.ENDED_PHASES:
                seen.add(step)
                queue.append((step, [*path, step]))
    raise AssertionError(f"fixture has no live path: {previous} -> {phase}")


def seed_observed_phase(conn, run_id, phase, now=None):
    """Seed a historical observation with no incoming edge in the frozen graph.

    The same-phase event records the fixture observation, not a fabricated
    transition. This does not establish that the live loop can reach the state.
    """
    with store.transaction(conn):
        conn.execute("UPDATE runs SET phase = ? WHERE id = ?", (phase, run_id))
        store.set_phase(conn, run_id, phase, note="fixture observation", now=now)


def finish_run(conn, run_id, outcome, *args, **kwargs):
    """Arrange the merge/rejection boundary before releasing a fixture run."""
    if outcome in {"merged", "rejected"}:
        current = store.run_phase(conn, run_id)
        if store.TERMINAL_PHASES[outcome] not in store.RUN_PHASE_TRANSITIONS[current]:
            phase = "merging" if outcome == "merged" else "merge_gate"
            advance_phase(conn, run_id, phase, now=kwargs.get("now"))
    return store.release(conn, run_id, outcome, *args, **kwargs)


def park_run(conn, run_id, phase, *args, **kwargs):
    """Prepare a parked snapshot for tests of readers and operator commands."""
    if phase == "blocked_on_operator":
        # No incoming edge exists: this is historical fixture data, not proof
        # that park() works from a live phase (the KO-580 scope conflict).
        seed_observed_phase(conn, run_id, phase, now=kwargs.get("now"))
    else:
        advance_phase(conn, run_id, "merge_gate", now=kwargs.get("now"))
    return store.park(conn, run_id, phase, *args, **kwargs)


def merged_task(target, task, conn=None, run_id=None, provider=None):
    """A run_task stub must leave its run at the boundary dispatch releases."""
    advance_phase(conn, run_id, "merging")
    return True
