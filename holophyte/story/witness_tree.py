"""The warm tree a project's loop witness passes reuse between passes."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from holophyte.config.worktree_settings import setup_steps
from holophyte.loop.gates import run_capped

TREE, LOCK, STAMPS = "witness-tree", "witness-tree.lock", "witness-tree.json"
MISSING_INPUT = "missing"


GLOB_SPECIAL = re.compile(r"([\\*?\[]|\s$)")


def clean_args(keep):
    return ["clean", "-q", "-ffdx",
            *(arg for path in keep for arg in ("-e", _literal(path)))]


def _literal(path):
    return "/" + GLOB_SPECIAL.sub(r"\\\1", path)


@contextlib.contextmanager
def warm_tree(target):
    holo_dir = Path(target.holo_dir)
    holo_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(holo_dir / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
        else:
            yield WarmTree(target)
    finally:
        os.close(fd)


class WarmTree:
    def __init__(self, target):
        self.target = target
        self.path = Path(target.holo_dir) / TREE
        self.stamps_path = Path(target.holo_dir) / STAMPS
        self.steps, self.keep = [], ()

    def prepare(self, sha, deadline):
        try:
            self.steps = setup_steps(self.target)
        except SystemExit as error:
            if not isinstance(error.code, str):
                raise
            return error.code
        self.keep = tuple(sorted({path for step in self.steps
                                  for path in step.keep}))
        if self._registered():
            failed = _git(["checkout", "-q", "--detach", "--force", sha],
                          self.path, deadline)
        else:
            failed = self._create(sha, deadline)
        return failed or _git(clean_args(self.keep), self.path, deadline)

    def reuse(self, sha):
        stamps, reused, ran = self._stamps(), set(), {}
        for number, step in enumerate(self.steps, 1):
            if step.inputs is None:
                continue
            key, name = self._key(sha, step), f"{number}: {step.run}"
            if (not ran and stamps.get(name) == key
                    and all(os.path.lexists(self.path / path)
                            for path in step.keep)):
                reused.add(number)
            else:
                ran[name] = key
        return reused, ran

    def record(self, ran):
        self._write({**self._stamps(), **ran})

    def clear(self):
        self._write({})

    def _key(self, sha, step):
        digest = hashlib.sha256(step.run.encode())
        for path in step.inputs:
            found = subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                                    f"{sha}:{path}"], cwd=self.path,
                                   capture_output=True, text=True)
            oid = found.stdout.strip() if found.returncode == 0 else MISSING_INPUT
            digest.update(f"\0{path}\0{oid}".encode())
        return digest.hexdigest()

    def _stamps(self):
        try:
            stamps = json.loads(self.stamps_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(stamps, dict) or not all(
                isinstance(value, str) for value in stamps.values()):
            return {}
        return stamps

    def _write(self, stamps):
        fd, temporary = tempfile.mkstemp(prefix=".witness-tree-",
                                         dir=self.stamps_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(stamps, stream, indent=2, sort_keys=True)
            os.replace(temporary, self.stamps_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _registered(self):
        if self.path.is_symlink() or not self.path.is_dir():
            return False
        listed = subprocess.run(["git", "worktree", "list", "--porcelain"],
                                cwd=self.target.path, capture_output=True,
                                text=True)
        mine = self.path.resolve()
        return listed.returncode == 0 and any(
            Path(line.removeprefix("worktree ")).resolve() == mine
            for line in listed.stdout.splitlines()
            if line.startswith("worktree "))

    def _create(self, sha, deadline):
        self._discard()
        failed = _git(["worktree", "add", "--detach", str(self.path), sha],
                      self.target.path, deadline)
        if failed:
            self._discard()
        return failed

    def _discard(self):
        subprocess.run(["git", "worktree", "remove", "--force", "--force",
                        str(self.path)], cwd=self.target.path,
                       capture_output=True)
        if self.path.is_symlink():
            self.path.unlink()
        shutil.rmtree(self.path, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=self.target.path,
                       capture_output=True)


def _git(args, cwd, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "[witness] the witness budget is spent\n"
    try:
        code, output = run_capped(["git", *args], cwd, remaining)
    except subprocess.TimeoutExpired:
        return f"[witness] git {args[0]} in the warm tree timed out\n"
    except OSError as error:
        return f"[witness] could not prepare the warm tree: {error}\n"
    if code:
        return output or f"git {args[0]} exited {code}\n"
    return None
