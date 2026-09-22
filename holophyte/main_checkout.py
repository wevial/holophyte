"""The detached main checkout the babysitter's main-side verify runs in.

A baseline that fails on missing packages is not a baseline (KO-643): the
checkout gets what a task worktree has before the verify runs. Each
`[worktree] carry` directory the task worktree holds is linked in at the
same relative path; a carry entry the task worktree lacks runs the target's
`[worktree] setup` in the checkout instead, through the claim's own runner
and timeout. The run event names what was carried and what ran.
"""
import tempfile
from contextlib import contextmanager
from pathlib import Path

import store
from holophyte.claim import run_worktree_setup
from holophyte.config import carry_directories, setup_commands
from holophyte.gates import sh
from holophyte.runs import heartbeat_while


@contextmanager
def detached_main(target, conn, run_id, beat_s, wt, sha):
    """A prepared detached checkout of `sha` beside `wt`, removed on exit
    -- its links first, since the directories behind them are `wt`'s."""
    wt = Path(wt)
    with tempfile.TemporaryDirectory(prefix="main-verify-", dir=wt.parent) as tmp:
        detached = Path(tmp) / "tree"
        sh(["git", "worktree", "add", "--detach", str(detached), sha], wt)
        links = []
        try:
            links = _prepare(target, conn, run_id, beat_s, wt, detached, sha)
            yield detached
        finally:
            for link in links:
                link.unlink()
            sh(["git", "worktree", "remove", "--force", str(detached)], wt)


def _prepare(target, conn, run_id, beat_s, wt, detached, sha):
    """Carry or set up `detached` like a task worktree; returns the links."""
    entries = carry_directories(target)
    carried = [entry for entry in entries if (wt / entry).is_dir()]
    missing = [entry for entry in entries if entry not in carried]
    links = []
    for entry in carried:
        link = detached / entry
        if not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to((wt / entry).resolve(), target_is_directory=True)
            links.append(link)
    notes = [f"carried {', '.join(carried)} from the task worktree"] if carried else []
    commands = setup_commands(target) if missing else []
    if commands:
        with heartbeat_while(conn, run_id, beat_s):
            ok, out = run_worktree_setup(target, detached)
        notes.append(f"ran setup for {', '.join(missing)}: {'; '.join(commands)}"
                     + ("" if ok else f" -- FAILED:\n{out}"))
    if notes and conn is not None and run_id is not None:
        store.record_event(conn, run_id, "verification",
                           f"main-side verify at {sha[:12]} prepared like a task"
                           f" worktree: {'; '.join(notes)}")
    return links
