"""A story's witness commands run at one commit of main, one verdict each."""
import collections
import hashlib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from holophyte import pr, redact
from holophyte.config_tables import merge_config, story_config
from holophyte.gates import _verify_command, run_capped, vacuous_green_report
from store.stories import record_witness_result, story, witness_ledger

UNITTEST_SUMMARY = re.compile(r"^FAILED \(([^)]*)\)\s*$", re.MULTILINE)
PYTEST_SUMMARY = re.compile(r"^(FAILED|ERROR) ([^(\s].*)$", re.MULTILINE)
PYTEST_NODE = re.compile(r"[^\s\[]+(?:\[.*?\])?(?: - (.*))?")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NO_COLOR = "export NO_COLOR=1 PYTHON_COLORS=0\n"
LOG_TAIL_BYTES = 64 * 1024
ABSENT, REFUSED = "absent", "refused"
Scratch = collections.namedtuple(
    "Scratch", ("target", "tree", "sha", "copied", "deadline", "secrets"))
SPENT = "[witness] not run: the witness budget is spent\n"


def main_tip(target):
    ref = pr.BASE
    if merge_config(target).mode == "pr":
        try:
            subprocess.run(["git", "fetch", pr.REMOTE, pr.BASE],
                           cwd=target.path, capture_output=True, text=True,
                           check=True, timeout=pr.PR_TIMEOUT,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"git fetch {pr.REMOTE} {pr.BASE} did not answer"
                               f" in {pr.PR_TIMEOUT}s") from None
        ref = f"{pr.REMOTE}/{pr.BASE}"
    return _git(target.path, "rev-parse", "--verify", f"{ref}^{{commit}}")


def red_kind(output):
    output = ANSI_ESCAPE.sub("", output)
    asserts = [
        "failures=" in counts and "errors=" not in counts
        for counts in UNITTEST_SUMMARY.findall(output)] + [
        status == "FAILED" and _pytest_message(rest).startswith(
            ("AssertionError", "assert "))
        for status, rest in PYTEST_SUMMARY.findall(output)]
    return "assert" if asserts and all(asserts) else "exception"


def _pytest_message(rest):
    node = PYTEST_NODE.fullmatch(rest)
    return (node and node.group(1)) or ""


def run_witnesses(target, conn, story_id, sha, verifier, copy_files=False):
    witnesses = story(conn, story_id).witnesses
    (identifier,) = conn.execute(
        "SELECT linearIdentifier FROM tickets WHERE id = ?",
        (story_id,)).fetchone()
    logs = Path(target.holo_dir) / "witness" / identifier / sha
    stamp = f"{verifier}-{time.time_ns()}"
    deadline = time.monotonic() + story_config(target).witness_sec
    secrets = redact.known_secrets(target.config())
    ids = []
    with tempfile.TemporaryDirectory(prefix="witness-") as scratch:
        tree = Path(scratch) / "tree"
        try:
            failed = _add_worktree(target, tree, sha, deadline)
            copied, found, failed = (
                ([], {}, failed) if failed
                else _checkout(tree, witnesses, copy_files))
            run = Scratch(target, tree, sha, copied, deadline, secrets)
            ran = False
            for witness in witnesses:
                log = logs / witness.key / f"{stamp}.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                state = found.get(witness.key)
                if state == ABSENT:
                    verdict = ("absent", None, None, None)
                elif failed or state == REFUSED:
                    _write_log(log, failed or _refusal(witness), secrets)
                    verdict = ("error", None, None, 0.0)
                else:
                    verdict = _judge(run, witness, state, log, ran)
                    ran = True
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
        return SPENT
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


def _inside(tree, file):
    if Path(file).is_absolute():
        return None
    path = (tree / file).resolve()
    return path if path.is_relative_to(tree.resolve()) else None


def _checkout(tree, witnesses, copy_files):
    try:
        copied = [witness for witness in witnesses
                  if copy_files and _inside(tree, witness.file)]
        _copy_sources(tree, copied)
        found = {witness.key: _presence(tree, witness.file)
                 for witness in witnesses}
    except (OSError, RuntimeError) as error:
        return [], {}, f"[witness] could not check the witness files out: {error}\n"
    return copied, found, None


def _presence(tree, file):
    path = _inside(tree, file)
    if path is None:
        return REFUSED
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ABSENT


def _copy_sources(tree, witnesses):
    for witness in witnesses:
        path = tree / witness.file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(witness.source)


def _restore(run):
    for command in (["git", "reset", "-q", "--hard", run.sha],
                    ["git", "clean", "-q", "-ffdx"]):
        remaining = run.deadline - time.monotonic()
        if remaining <= 0:
            return SPENT
        try:
            code, output = run_capped(command, run.tree, remaining)
        except subprocess.TimeoutExpired:
            return f"[witness] {' '.join(command[:2])} timed out\n"
        if code:
            return output or f"{' '.join(command[:2])} exited {code}\n"
    try:
        _copy_sources(run.tree, run.copied)
    except OSError as error:
        return f"[witness] could not copy the witness files back: {error}\n"
    return None


def _judge(run, witness, file_hash, log, restore):
    failed = _restore(run) if restore else None
    if failed:
        _write_log(log, failed, run.secrets)
        return "error", None, file_hash, 0.0
    return _run_one(run, witness, file_hash, log)


def _refusal(witness):
    return (f"[witness] refused: {witness.file} is absolute or resolves"
            " outside the checkout\n")


def _write_log(log, text, secrets):
    data = redact.outbound(text, secrets).encode()
    if len(data) > LOG_TAIL_BYTES:
        data = (f"[witness] log cut to its last {LOG_TAIL_BYTES} bytes\n"
                .encode() + data[-LOG_TAIL_BYTES:])
    log.write_bytes(data)


def _run_one(run, witness, file_hash, log):
    secrets = run.secrets
    started = time.monotonic()
    remaining = run.deadline - started
    if remaining <= 0:
        _write_log(log, SPENT, secrets)
        return "error", None, file_hash, 0.0
    try:
        code, output = _verify_command(run.target, NO_COLOR + witness.command,
                                       run.tree, remaining)
    except subprocess.TimeoutExpired as expired:
        _write_log(log, f"{expired.output or ''}\n[witness] timed out after"
                        f" {remaining:.0f}s\n", secrets)
        return "error", None, file_hash, time.monotonic() - started
    except OSError as error:
        _write_log(log, f"[witness] could not run: {error}\n", secrets)
        return "error", None, file_hash, time.monotonic() - started
    output = output or ""
    seconds = time.monotonic() - started
    vacuous = vacuous_green_report(witness.command, output) if code == 0 else None
    _write_log(log, vacuous or output, secrets)
    if vacuous:
        return "error", None, file_hash, seconds
    if code == 0:
        return "green", None, file_hash, seconds
    return "red", red_kind(output), file_hash, seconds


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()
