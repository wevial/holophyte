from typing import Protocol


class Locks(Protocol):
    def merge(self, conn, run_id, beat_s, operation="gate",
              wait_phase="merge_gate"):
        pass


class FileLocks:
    def __init__(self, target):
        self.target = target

    def merge(self, conn, run_id, beat_s, operation="gate",
              wait_phase="merge_gate"):
        # Deferred so building a `Project` imports neither the gates nor the store.
        from holophyte.loop.merge_lock import live_merge_lock
        return live_merge_lock(self.target, conn, run_id, beat_s,
                               operation=operation, wait_phase=wait_phase)
