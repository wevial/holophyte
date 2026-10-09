"""A story's witness commands run at one commit of main, one verdict each."""
import collections
import hashlib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from holophyte import deadline, redact
from holophyte.admission import held_line
from holophyte.config.config_tables import merge_config, story_config
from holophyte.loop.gates import _verify_command, run_capped, vacuous_green_report
from holophyte.pr import github
from holophyte.redact import safe_print as print
from holophyte.story.story_close import (
    main_ledger,
    rerun_owed,
    settle_owed,
    settle_story,
)
from store.notes import record_note
from store.stories import OPEN_STATES, record_witness_result, story, witness_ledger

UNITTEST_SUMMARY = re.compile(r"^FAILED \(([^)]*)\)\s*$", re.MULTILINE)
PYTEST_SUMMARY = re.compile(r"^(FAILED|ERROR) ([^(\s].*)$", re.MULTILINE)
PYTEST_NODE = re.compile(r"[^\s\[]+(?:\[.*?\])?(?: - (.*))?")
GO_RESULT = re.compile(r"^(ok|FAIL)[ \t]+\S.*$", re.MULTILINE)
GO_TEST_FAILED = re.compile(r"^[ \t]*--- FAIL:", re.MULTILINE)
GO_EXCEPTION = re.compile(r"^(?:panic: |WARNING: DATA RACE[ \t]*$)", re.MULTILINE)
GO_UNBUILT = re.compile(r"\[(?:build|setup) failed\]")
PLAYWRIGHT_FAILED = re.compile(r"^[ \t]*\d+ failed[ \t]*$", re.MULTILINE)
PLAYWRIGHT_TEST = re.compile(r"^[ \t]*\d+\) \S.* › .*$", re.MULTILINE)
PLAYWRIGHT_ERROR = re.compile(r"^[ \t]*\w*Error: .*$", re.MULTILINE)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NO_COLOR = "export NO_COLOR=1 PYTHON_COLORS=0\n"
LOG_TAIL_BYTES = 64 * 1024
ABSENT, REFUSED = "absent", "refused"
Scratch = collections.namedtuple(
    "Scratch", ("target", "tree", "sha", "copied", "deadline", "secrets"))
SPENT = "[witness] not run: the witness budget is spent\n"
TIP_FAILURES = (OSError, RuntimeError, subprocess.SubprocessError)


def main_tip(target):
    ref = github.BASE
    if merge_config(target).mode == "pr":
        try:
            subprocess.run(["git", "fetch", github.REMOTE, github.BASE],
                           cwd=target.path, capture_output=True, text=True,
                           check=True, timeout=github.PR_TIMEOUT,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"git fetch {github.REMOTE} {github.BASE} did not answer"
                               f" in {github.PR_TIMEOUT}s") from None
        ref = f"{github.REMOTE}/{github.BASE}"
    return _git(target.path, "rev-parse", "--verify", f"{ref}^{{commit}}")


def red_kind(output):
    output = ANSI_ESCAPE.sub("", output)
    asserts = [
        "failures=" in counts and "errors=" not in counts
        for counts in UNITTEST_SUMMARY.findall(output)] + [
        status == "FAILED" and _pytest_message(rest).startswith(
            ("AssertionError", "assert "))
        for status, rest in PYTEST_SUMMARY.findall(output)]
    asserts += _go_asserts(output) + _playwright_asserts(output)
    return "assert" if asserts and all(asserts) else "exception"


def _go_asserts(output):
    asserts, start = [], 0
    for result in GO_RESULT.finditer(output):
        block, start = output[start:result.end()], result.end()
        if result.group(1) == "FAIL":
            asserts.append(bool(GO_TEST_FAILED.search(block))
                           and not GO_UNBUILT.search(result.group())
                           and not GO_EXCEPTION.search(block))
    return asserts


def _playwright_asserts(output):
    asserts, start = [], 0
    for summary in PLAYWRIGHT_FAILED.finditer(output):
        tests = list(PLAYWRIGHT_TEST.finditer(output, start, summary.start()))
        ends = [test.start() for test in tests[1:]] + [summary.start()]
        for test, end in zip(tests, ends):
            error = PLAYWRIGHT_ERROR.search(output, test.end(), end)
            asserts.append(bool(error) and error.group().lstrip().startswith(
                "Error: expect("))
        start = summary.end()
    return asserts


def _pytest_message(rest):
    node = PYTEST_NODE.fullmatch(rest)
    return (node and node.group(1)) or ""


def run_witnesses(target, conn, story_id, sha, verifier, copy_files=False,
                  again=None):
    witnesses = story(conn, story_id).witnesses
    (identifier,) = conn.execute(
        "SELECT linearIdentifier FROM tickets WHERE id = ?",
        (story_id,)).fetchone()
    logs = Path(target.holo_dir) / "witness" / identifier / sha
    stamp = f"{verifier}-{time.time_ns()}"
    deadline = time.monotonic() + story_config(target).witness_sec
    secrets = redact.known_secrets(target.config())
    with tempfile.TemporaryDirectory(prefix="witness-") as scratch:
        tree = Path(scratch) / "tree"
        try:
            failed = _add_worktree(target, tree, sha, deadline)
            copied, found, failed = (
                ([], {}, failed) if failed
                else _checkout(tree, witnesses, copy_files))
            run = Scratch(target, tree, sha, copied, deadline, secrets)
            judge = _Judge(run, conn, story_id, verifier, found, failed)
            ids = [judge.record(witness, logs / witness.key / f"{stamp}.log")
                   for witness in witnesses]
            rerun = again(_rows(conn, story_id, ids)) if again else ()
            ids += [judge.record(witness,
                                 logs / witness.key / f"{stamp}-again.log")
                    for witness in witnesses
                    if witness.key in rerun and time.monotonic() < deadline]
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", "--force",
                            str(tree)], cwd=target.path, capture_output=True)
    return _rows(conn, story_id, ids)


def _rows(conn, story_id, ids):
    return [row for row in witness_ledger(conn, story_id) if row.id in ids]


class _Judge:
    def __init__(self, run, conn, story_id, verifier, found, failed):
        self.run, self.conn, self.story_id = run, conn, story_id
        self.verifier, self.found, self.failed = verifier, found, failed
        self.ran = False

    def record(self, witness, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        state = self.found.get(witness.key)
        if state == ABSENT:
            verdict = ("absent", None, None, None)
        elif self.failed or state == REFUSED:
            _write_log(log, self.failed or _refusal(witness), self.run.secrets)
            verdict = ("error", None, None, 0.0)
        else:
            verdict = _judge(self.run, witness, state, log, self.ran)
            self.ran = True
        verdict_name, kind, file_hash, seconds = verdict
        return record_witness_result(
            self.conn, self.story_id, witness.key, self.run.sha, verdict_name,
            self.verifier, red_kind=kind, file_hash=file_hash,
            evidence_path=str(log) if log.exists() else None, seconds=seconds)


def witness_pass(target, conn, story_id, verifier):
    if pass_refusal(conn, story_id) is not None:
        return []
    sha = main_tip(target)
    keys = {witness.key for witness in story(conn, story_id).witnesses}
    at_tip = {row.witnessKey for row in main_ledger(conn, story_id, sha)}
    if (verifier != "operator" and keys <= at_tip
            and not rerun_owed(conn, story_id, sha)):
        settle_story(target, conn, story_id, sha)
        return []
    before = [row for row in main_ledger(conn, story_id)
              if row.mainSha != sha]
    greens = {row.witnessKey for row in before if row.verdict == "green"}

    def again(rows):
        return {row.witnessKey for row in rows
                if row.verdict == "red" and row.witnessKey in greens}

    rows = run_witnesses(target, conn, story_id, sha, verifier, again=again)
    _note_changes(conn, story_id, sha, before, rows)
    settle_story(target, conn, story_id, sha)
    return rows


def pass_pending(target, conn, project_id):
    if held_line(conn, project_id):
        return []
    stories = [story(conn, story_id) for (story_id,) in conn.execute(
        "SELECT s.ticketId FROM stories s JOIN tickets t ON t.id = s.ticketId"
        " WHERE t.projectId = ? AND s.state IN (?, ?) ORDER BY s.ticketId",
        (project_id, *OPEN_STATES)).fetchall()]
    if not stories:
        return []
    deadline.check("main's tip for a witness pass")
    sha = main_tip(target)
    return [found.ticketId for found in stories
            if {witness.key for witness in found.witnesses}
            - {row.witnessKey for row in main_ledger(conn, found.ticketId,
                                                     sha)}
            or rerun_owed(conn, found.ticketId, sha)
            or settle_owed(conn, found.ticketId, sha)]


def witness_step(target, conn, project_id):
    try:
        for story_id in pass_pending(target, conn, project_id):
            rows = witness_pass(target, conn, story_id, "loop")
            if rows:
                print(f"[holo2] witness pass at {rows[0].mainSha}: "
                      + ", ".join(f"{row.witnessKey} {row.verdict}"
                                  for row in rows))
    except TIP_FAILURES as error:
        print(f"[holo2] the witness pass could not run ({error}); the next"
              " pass tries again")


def pass_refusal(conn, story_id):
    found = story(conn, story_id)
    if found is None or found.ticketId != story_id:
        return "not a story's parent"
    if found.state not in OPEN_STATES:
        return f"the story is {found.state}, not approved or parked"
    (project_id,) = conn.execute("SELECT projectId FROM tickets WHERE id = ?",
                                 (story_id,)).fetchone()
    line = held_line(conn, project_id)
    return line and line.removeprefix("[holo2] ")


def _note_changes(conn, story_id, sha, before, rows):
    previous = {row.witnessKey: row for row in before}
    now = {row.witnessKey: row for row in rows}
    for key, row in sorted(now.items()):
        was = previous.get(key)
        if was is None or was.verdict == row.verdict:
            continue
        record_note(conn, story_id, "verdict",
                    f"Witness {key} is {row.verdict} at {sha}, was"
                    f" {was.verdict} at {was.mainSha}.",
                    f"witness:{story_id}:{key}:{sha}")


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
        except OSError as error:
            return f"[witness] could not restore the checkout: {error}\n"
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
