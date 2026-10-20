import store
import store.read
from holophyte.host.sweep_report import finished_worktrees
from holophyte.loop.task_worktree import retire_worktree


def retire_finished(target, conn):
    lines = []
    for identifier, _status, branch, _path, run_id in finished_worktrees(
            target, conn, ended=True):
        reason = retire_worktree(target, branch)
        if reason is None:
            lines.append(f"retired {identifier}'s worktree and branch {branch}")
            continue
        line = f"kept {identifier}'s worktree: {reason}"
        if store.read.latest_event_summary(
                conn, run_id, "worktree_retirement_refused") != line:
            store.record_event(conn, run_id, "worktree_retirement_refused",
                               line)
        lines.append(line)
    return lines
