import tempfile
from contextlib import contextmanager
from pathlib import Path

import store
from holophyte.config.worktree_settings import carry_directories, setup_commands
from holophyte.loop.claim import run_worktree_setup
from holophyte.loop.gates import sh
from holophyte.loop.runs import heartbeat_while


@contextmanager
def detached_main(target, conn, run_id, beat_s, wt, sha):
    """On exit the links go first: the directories behind them are `wt`'s."""
    wt = Path(wt)
    with tempfile.TemporaryDirectory(prefix="main-verify-", dir=wt.parent) as tmp:
        detached = Path(tmp) / "tree"
        sh(["git", "worktree", "add", "--detach", str(detached), sha], wt)
        links = []
        try:
            links, setup_failure = _prepare(target, conn, run_id, beat_s, wt,
                                            detached, sha)
            yield detached, setup_failure
        finally:
            try:
                # Setup may have replaced a link with a directory of its own.
                for link in links:
                    if link.is_symlink():
                        link.unlink()
            finally:
                sh(["git", "worktree", "remove", "--force", str(detached)], wt)


def _prepare(target, conn, run_id, beat_s, wt, detached, sha):
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
    commands = setup_commands(target) if missing or not entries else []
    setup_failure = None
    if commands:
        with heartbeat_while(conn, run_id, beat_s):
            ok, out = run_worktree_setup(target, detached)
        setup_failure = None if ok else out
        scope = f" for {', '.join(missing)}" if missing else ""
        notes.append(f"ran setup{scope}: {'; '.join(commands)}"
                     + ("" if ok else f" -- FAILED:\n{out}"))
    if notes and conn is not None and run_id is not None:
        store.record_event(conn, run_id, "verification",
                           f"main-side verify at {sha[:12]} prepared like a task"
                           f" worktree: {'; '.join(notes)}")
    return links, setup_failure
