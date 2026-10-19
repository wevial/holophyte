import store
from holophyte.host.sweep_report import finished_worktrees
from holophyte.loop.task_worktree import retire_worktree


def retire_finished(target, conn):
    lines = []
    for identifier, status, branch, _path, run_id in finished_worktrees(
            target, conn, ended=True):
        reason = retire_worktree(target, branch, merged=status == "merged")
        if reason is None:
            lines.append(f"retired {identifier}'s worktree and branch {branch}")
            continue
        line = f"kept {identifier}'s worktree: {reason}"
        store.record_event(conn, run_id, "worktree_retirement_refused", line)
        lines.append(line)
    return lines
