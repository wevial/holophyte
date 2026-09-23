"""The locks a target is held under, reached through the target (KO-594).

The factory holds its leases and locks across three media -- store rows, a
Linear label, lock files -- and each caller used to name the mechanism it
took. `Project.locks` names the intent instead, so a second host, a
single-process scheduler or a test can hand a target another `Locks`
without touching the callers. The merge lock is the first lock behind it;
the supervisor lock, the store lease, the board label and the pool handoff
file are still taken where they are needed.
"""
from typing import Protocol


class Locks(Protocol):
    def merge(self, conn, run_id, beat_s, operation="gate",
              wait_phase="merge_gate"):
        """The merge lock for `run_id`, held for the `with` block."""


class FileLocks:
    """The lock files under the target's state directory: the merge lock is
    `live_merge_lock()` unchanged, heartbeat and wait phase included."""

    def __init__(self, target):
        self.target = target

    def merge(self, conn, run_id, beat_s, operation="gate",
              wait_phase="merge_gate"):
        # Deferred so building a `Project` imports neither the gates nor the
        # store, as `holophyte.project` promises.
        from holophyte.merge_lock import live_merge_lock
        return live_merge_lock(self.target, conn, run_id, beat_s,
                               operation=operation, wait_phase=wait_phase)
