"""Hold Git's index and ref locks while publishing a container turn."""

import contextlib
import shutil
import subprocess
from pathlib import Path

from holophyte.gates import InfraFailure
from holophyte.isolation_git import atomic_copy, git, git_environment


def transaction_command(process, command):
    process.stdin.write(command + "\n")
    process.stdin.flush()
    if process.stdout.readline().strip() != command + ": ok":
        raise InfraFailure(
            "task branch changed or ref update failed; refusing fast-forward: "
            + process.stderr.read().strip()
        )


@contextlib.contextmanager
def locked_return(worktree, root, old, sha):
    index = Path(git(worktree, "rev-parse", "--path-format=absolute",
                     "--git-path", "index"))
    lock = index.with_name(index.name + ".lock")
    try:
        descriptor = lock.open("xb")
    except FileExistsError as error:
        raise InfraFailure("task worktree index is locked; refusing return") from error
    try:
        with descriptor:
            if sha != old:
                prepared = root / "return-index"
                git(worktree, "read-tree", sha, index=prepared)
        with subprocess.Popen(
            ["git", "-c", "core.hooksPath=/dev/null", "update-ref", "--stdin"],
            cwd=worktree, env=git_environment(), text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as process:
            try:
                transaction_command(process, "start")
                process.stdin.write(f"update HEAD {sha} {old}\n")
                # prepare checks old and holds HEAD and branch locks until commit.
                transaction_command(process, "prepare")

                def finish():
                    original = root / "original-index"
                    if index.exists():
                        shutil.copyfile(index, original)
                    try:
                        if sha != old:
                            atomic_copy(prepared, index)
                        transaction_command(process, "commit")
                    except BaseException:
                        if original.exists():
                            atomic_copy(original, index)
                        else:
                            index.unlink(missing_ok=True)
                        raise

                yield finish
            finally:
                # EOF aborts any still-prepared transaction, including on signals.
                process.stdin.close()
                process.wait()
    finally:
        lock.unlink(missing_ok=True)
