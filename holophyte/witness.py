"""A story's witness commands run at one commit of main, one verdict each."""
import hashlib
import re
import subprocess
import tempfile
import time
from pathlib import Path

from holophyte import pr
from holophyte.config_tables import merge_config, story_config
from holophyte.gates import _verify_command, run_capped
from store.stories import record_witness_result, story, witness_ledger

UNITTEST_SUMMARY = re.compile(r"^FAILED \(([^)]*)\)\s*$", re.MULTILINE)
PYTEST_SUMMARY = re.compile(r"^(FAILED|ERROR) [^(\s].*$", re.MULTILINE)


def main_tip(target):
    ref = pr.BASE
    if merge_config(target).mode == "pr":
        subprocess.run(["git", "fetch", pr.REMOTE, pr.BASE], cwd=target.path,
                       capture_output=True, text=True, check=True)
        ref = f"{pr.REMOTE}/{pr.BASE}"
    return _git(target.path, "rev-parse", "--verify", f"{ref}^{{commit}}")


def red_kind(output):
    summaries = UNITTEST_SUMMARY.findall(output)
    if summaries:
        counts = summaries[-1]
        return ("assert" if "failures=" in counts and "errors=" not in counts
                else "exception")
    lines = list(PYTEST_SUMMARY.finditer(output))
    if lines and all(line.group(1) == "FAILED"
                     and "AssertionError" in line.group() for line in lines):
        return "assert"
    return "exception"


def run_witnesses(target, conn, story_id, sha, verifier, copy_files=False):
    witnesses = story(conn, story_id).witnesses
    (identifier,) = conn.execute(
        "SELECT linearIdentifier FROM tickets WHERE id = ?",
        (story_id,)).fetchone()
    logs = Path(target.holo_dir) / "witness" / identifier / sha
    stamp = f"{verifier}-{time.time_ns()}"
    deadline = time.monotonic() + story_config(target).witness_sec
    ids = []
    with tempfile.TemporaryDirectory(prefix="witness-") as scratch:
        tree = Path(scratch) / "tree"
        try:
            failed = _add_worktree(target, tree, sha, deadline)
            if failed is None and copy_files:
                _copy_sources(tree, witnesses)
            for witness in witnesses:
                log = logs / witness.key / f"{stamp}.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                if failed is not None:
                    log.write_text(failed)
                    verdict = ("error", None, None, 0.0)
                else:
                    verdict = _run_one(target, tree, witness, deadline, log)
                verdict_name, kind, file_hash, seconds = verdict
                ids.append(record_witness_result(
                    conn, story_id, witness.key, sha, verdict_name, verifier,
                    red_kind=kind, file_hash=file_hash,
                    evidence_path=str(log) if log.exists() else None,
                    seconds=seconds))
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", "--force",
                            str(tree)], cwd=target.path, capture_output=True)
    return [row for row in witness_ledger(conn, story_id) if row.id in ids]



def _add_worktree(target, tree, sha, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "[witness] not run: the witness budget is spent\n"
    try:
        code, output = run_capped(
            ["git", "worktree", "add", "--detach", str(tree), sha],
            target.path, remaining)
    except subprocess.TimeoutExpired as expired:
        return (f"{expired.output or ''}\n[witness] worktree add timed out"
                f" after {remaining:.0f}s\n")
    if code:
        return output or f"git worktree add exited {code}\n"
    return None


def _copy_sources(tree, witnesses):
    for witness in witnesses:
        path = tree / witness.file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(witness.source)


def _run_one(target, tree, witness, deadline, log):
    path = tree / witness.file
    if not path.is_file():
        return "absent", None, None, None
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    started = time.monotonic()
    remaining = deadline - started
    if remaining <= 0:
        log.write_text("[witness] not run: the witness budget is spent\n")
        return "error", None, file_hash, 0.0
    try:
        code, output = _verify_command(target, witness.command, tree,
                                       remaining)
    except subprocess.TimeoutExpired as expired:
        log.write_text(f"{expired.output or ''}\n[witness] timed out after"
                       f" {remaining:.0f}s\n")
        return "error", None, file_hash, time.monotonic() - started
    except OSError as error:
        log.write_text(f"[witness] could not run: {error}\n")
        return "error", None, file_hash, time.monotonic() - started
    log.write_text(output or "")
    seconds = time.monotonic() - started
    if code == 0:
        return "green", None, file_hash, seconds
    return "red", red_kind(output or ""), file_hash, seconds


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()
