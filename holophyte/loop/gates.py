import contextlib
import fcntl
import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from time import monotonic, sleep, time

import ticket_template
from holophyte.config.reader import VERIFY_TIMEOUT
from holophyte.config.worktree_settings import carry_directories

DEFAULT_BUDGET_MIN = 20


TASK_RE = re.compile(r"^[-*] \[ \] (.+)$", re.M)
BUDGET_RE = re.compile(r"\((\d+)\s*min\)\s*$")
VERIFY_RE = re.compile(r"\(verify:\s*(.+?)\)\s*$")


def parse_task(line):
    text = line.strip()
    budget, verify = DEFAULT_BUDGET_MIN, None
    m = BUDGET_RE.search(text)
    if m:
        budget = int(m.group(1))
        text = BUDGET_RE.sub("", text).strip()
    m = VERIFY_RE.search(text)
    if m:
        verify = m.group(1).strip()
        text = VERIFY_RE.sub("", text).strip()
    return text, verify, budget


CLAUSE_MARK = "__holo2_verify_clause__"
FAIL_MARK = "__holo2_verify_failed__"

VACUOUS_RE = re.compile(r"^\s*(?:Ran 0 tests\b|collected 0 items\b)", re.M)


def split_and_clauses(cmd, *, allow_or=False):  # noqa: C901 - hand-written shell tokenizer
    if "<<" in cmd:
        return None
    clauses, buf = [], []
    quote, depth, escaped = None, 0, False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "`":
            return None
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        elif ch == "#" and (i == 0 or cmd[i - 1].isspace() or cmd[i - 1] in "&("):
            # Only a trailing comment is safe: a newline resumes code, and
            # inside `(...)` the comment would swallow the closer.
            if depth or "\n" in cmd[i:]:
                return None
            buf.append(cmd[i:])
            break
        elif depth == 0:
            prev = next((c for c in reversed(buf) if not c.isspace()), "")
            if cmd[i:i + 2] == "&&":
                clauses.append("".join(buf).strip())
                buf = []
                i += 2
                continue
            if ch == "&" and prev not in "><":
                return None
            if (cmd[i:i + 2] == "||" and not allow_or) or ch in ";\n":
                return None
        buf.append(ch)
        i += 1
    if quote or escaped or depth:
        return None
    clauses.append("".join(buf).strip())
    return None if any(not c for c in clauses) else clauses


def instrumented_script(clauses, *, stop_on_failure=True):
    parts = ["__holo2_clause=0",
             "trap '__holo2_rc=$?; [ \"$__holo2_rc\" -eq 0 ] || "
             "printf \"%s\\n\" \"{} $__holo2_clause $__holo2_rc\"' EXIT"
             .format(FAIL_MARK)]
    if not stop_on_failure:
        # Reporting must not clobber the status a following `$?` reads.
        parts.append('__holo2_mark() { local status=$1; __holo2_clause=$2; '
                     f'printf "%s\\n" "{CLAUSE_MARK} $2"; return "$status"; }}')
    for idx, clause in enumerate(clauses, 1):
        if stop_on_failure:
            parts.append("__holo2_clause={}".format(idx))
            parts.append("printf '%s\\n' '{} {}'".format(CLAUSE_MARK, idx))
        else:
            parts.append(f'__holo2_mark $? {idx}')
        parts.append("{{ {}\n}}".format(clause) +
                     (" || exit $?" if stop_on_failure else ""))
    return "\n".join(parts)


def parse_clause_output(output):
    per_clause, failed, cleaned, current = {}, None, [], None

    def emit(text):
        cleaned.append(text)
        if current is not None:
            per_clause[current].append(text)

    for line in output.splitlines():
        cut = len(line)
        for mark in (CLAUSE_MARK, FAIL_MARK):
            pos = line.find(mark)
            if pos > 0:
                cut = min(cut, pos)
        if cut < len(line):
            emit(line[:cut])
            line = line[cut:]
        if line.startswith(CLAUSE_MARK + " "):
            current = int(line.split()[1])
            per_clause[current] = []
            continue
        if line.startswith(FAIL_MARK + " "):
            _, idx, rc = line.split()
            failed = (int(idx), int(rc))
            continue
        emit(line)
    return ({k: "\n".join(v) for k, v in per_clause.items()},
            failed, "\n".join(cleaned))


def failure_report(cmd, clauses, per_clause, failed, returncode, cleaned):
    if not (failed and clauses and 1 <= failed[0] <= len(clauses)):
        body = cleaned.strip() or "(no output — the command failed silently)"
        return (f"[verify] FAILED: command exited {returncode}\n"
                f"[verify]   full command: {cmd}\n"
                f"[verify]   output:\n{body[-2000:]}")
    idx, rc = failed
    head = (f"[verify] FAILED: clause {idx} of {len(clauses)} exited {rc}\n"
            f"[verify]   full command: {cmd}\n"
            f"[verify]   failing clause: {clauses[idx - 1]}")
    lines = []
    for n in range(1, idx + 1):
        status = f"exit {rc}" if n == idx else "ok"
        lines.append(f"[verify]   --- clause {n} ({status}): {clauses[n - 1]}")
        lines.append(per_clause.get(n, "").strip() or (
            "(no output — the clause failed silently)" if n == idx
            else "(no output)"))
    if idx < len(clauses):
        lines.append("[verify]   not executed: clause " + ", ".join(
            str(n) for n in range(idx + 1, len(clauses) + 1)))
    return f"{head}\n" + "\n".join(lines)[-2000:]


TIMEOUT_HEAD = "[verify] FAILED: verify timed out after "


def verify_timed_out(out):
    return str(out).startswith(TIMEOUT_HEAD)


def timeout_failure_report(cmd, clauses, per_clause, cleaned, timeout):
    running = max(per_clause) if per_clause else None
    head = f"{TIMEOUT_HEAD}{timeout:g}s"
    if not (clauses and running and 1 <= running <= len(clauses)):
        body = cleaned.strip() or "(no output before the timeout)"
        return (f"{head}\n"
                f"[verify]   full command: {cmd}\n"
                f"[verify]   output:\n{body[-2000:]}")
    head += (f" in clause {running} of {len(clauses)}\n"
             f"[verify]   full command: {cmd}\n"
             f"[verify]   running clause: {clauses[running - 1]}")
    lines = []
    for n in range(1, running + 1):
        status = "timed out" if n == running else "ok"
        lines.append(f"[verify]   --- clause {n} ({status}): {clauses[n - 1]}")
        lines.append(per_clause.get(n, "").strip() or (
            "(no output before the timeout)" if n == running
            else "(no output)"))
    if running < len(clauses):
        lines.append("[verify]   not executed: clause " + ", ".join(
            str(n) for n in range(running + 1, len(clauses) + 1)))
    return f"{head}\n" + "\n".join(lines)[-2000:]


def vacuous_green_report(cmd, cleaned):
    m = VACUOUS_RE.search(cleaned)
    if not m:
        return None
    summary = cleaned[m.start():].splitlines()[0].strip()
    body = cleaned.strip() or "(no output)"
    return (f"[verify] FAILED: vacuous-green — exited 0 but ran no tests\n"
            f"[verify]   zero-test summary: {summary}\n"
            f"[verify]   full command: {cmd}\n"
            f"[verify]   output:\n{body[-2000:]}")


# A pipeline stays one clause: dropping `unittest` must not leave its `| tail`.
_UNITTEST_ARGS = re.compile(r"-m\s+unittest\b([^;&|\n]*)")
_TEST_MODULE = re.compile(r"\s+tests\.(\w+)[\w.]*(?=\s|$)")
_CLAUSE_OPERATOR = re.compile(r"(&&|\|\||;)")


def _drop_clause_modules(clause, candidate, main):
    found = _UNITTEST_ARGS.search(clause)
    args = found.group(1) if found else ""
    named = list(_TEST_MODULE.finditer(args))
    gone = [m for m in named
            if (candidate / "tests" / f"{m.group(1)}.py").exists()
            and not (main / "tests" / f"{m.group(1)}.py").exists()]
    names = [m.group(0).strip() for m in gone]
    if not gone or len(gone) == len(named):
        return (None if gone else clause), names
    for m in reversed(gone):
        args = args[:m.start()] + args[m.end():]
    return clause[:found.start(1)] + args + clause[found.end(1):], names


def drop_candidate_modules(command, candidate, main):
    lines, skipped = [], []
    for line in (command or "").splitlines():
        parts = _CLAUSE_OPERATOR.split(line)
        kept = []
        for index in range(0, len(parts), 2):
            clause, names = _drop_clause_modules(parts[index], candidate, main)
            skipped += names
            if clause is not None:
                kept.append((parts[index - 1] if index else "", clause))
        if len(kept) == len(parts[::2]):
            lines.append("".join(op + clause for op, clause in kept))
        elif kept:
            lines.append(kept[0][1].strip() + "".join(
                f" {op} {clause.strip()}" for op, clause in kept[1:]))
    return ("\n".join(lines) if skipped else command), skipped


def contract_report(contracts, cwd):
    """Never echoes the file: a declaration may point at one holding credentials."""
    root = Path(cwd)
    for path, literal in contracts or ():
        problem = ticket_template.contract_path_problem(path)
        if problem is None and not literal:
            problem = "declaration has an empty expected literal"
        declared = root / path
        if problem is None and not declared.is_file():
            problem = "declared file does not exist"
        if problem is None:
            if literal in declared.read_text(errors="replace"):
                continue
            problem = "expected literal is absent from the file"
        return (f"[verify] FAILED: contract check — {problem}\n"
                f"[verify]   path: {path}\n"
                f"[verify]   expected literal: {literal}")
    return None


REAP_GRACE = 10


def reap_group(proc, expired):
    """A grandchild in its own session can hold the pipe, so the last read is capped."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        out, _ = proc.communicate(timeout=REAP_GRACE)
    except subprocess.TimeoutExpired:
        # CPython attaches the partial output as bytes even under `text=True`.
        out = expired.output or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
    return out


class GroupKill:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self.wanted = False
        self.fired = False

    def arm(self, proc):
        with self._lock:
            self._proc = proc
            if self.wanted:
                self._kill()

    def __call__(self):
        with self._lock:
            self.wanted = True
            if self._proc is not None:
                self._kill()

    def _kill(self):
        self.fired = True
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            self._proc.kill()


def run_capped(cmd, cwd, timeout, on_start=None, *, env=None,
               stderr=subprocess.STDOUT):
    environment = {} if env is None else {"env": env}
    # A session of its own makes the command's whole tree one killable group.
    with subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=str(cwd),
                          stdout=subprocess.PIPE, stderr=stderr,
                          text=True, start_new_session=True, **environment) as proc:
        if on_start is not None:
            on_start(proc)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            raise subprocess.TimeoutExpired(
                cmd, timeout, output=reap_group(proc, expired)) from None
        return proc.returncode, out


def run_verify(cmd, cwd, contracts=None, timeout=None, *, conn=None, run_id=None,
               project=None):
    from store.working import working

    with working(conn, run_id, verify=True):
        return _run_verify(cmd, cwd, contracts, timeout, project=project,
                           run_id=run_id)


# Failures are never recorded; the worktree keeps another store's run 1 apart.
_PASSES = set()


def _pass_key(run_id, cmd, cwd):
    if run_id is None:
        return None
    try:
        head, main = subprocess.run(
            ["git", "rev-parse", "HEAD", "main"], cwd=cwd, capture_output=True,
            text=True, check=True).stdout.split()
        # `status.showUntrackedFiles = no` would hide a tree the head does not name.
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    return None if dirty else (run_id, str(Path(cwd).resolve()), head, main, cmd)


def _verify_command(project, command, cwd, timeout):
    from holophyte.isolation import launcher

    route = launcher.route_for(project) if project is not None else launcher.Route()
    argv = ['/bin/sh', '-c', command] if route.backend == 'container' else command
    env = launcher.environment(project) if project is not None else None
    carry = carry_directories(project) if project is not None else None
    return launcher.launch(route, cwd, env, argv, timeout=timeout, runner=run_capped,
                            carry=carry)


def _run_verify(cmd, cwd, contracts=None, timeout=None, *, project=None,
                run_id=None):
    drifted = contract_report(contracts, cwd)
    if drifted:
        return False, drifted
    passed = (f"[verify] contract checks passed: {len(contracts)}\n"
              if contracts else "")
    if not cmd:
        return True, passed + "(no verify command)"
    key = _pass_key(run_id, cmd, cwd)
    if key in _PASSES:
        return True, passed + (
            "[verify] not run again: passed earlier in this run at head"
            f" {key[2][:12]} with main at {key[3][:12]}")
    ok, out = _run_command(cmd, cwd, timeout, project)
    if ok and key is not None:
        _PASSES.add(key)
    return ok, passed + out if ok else out


def _run_command(cmd, cwd, timeout, project):
    # A compound list runs verbatim: wrapping it in `||` suppresses its errexit.
    lines = [text for text in cmd.splitlines()
             if text.strip() and not text.lstrip().startswith('#')]
    block = len(lines) > 1 and all(
        split_and_clauses(text, allow_or=True) is not None
        and subprocess.run(
            ['bash', '-n'], input=text, text=True, capture_output=True).returncode == 0
        for text in lines)
    clauses = lines if block else split_and_clauses(cmd)
    marked = bool(clauses) and len(clauses) > 1
    try:
        returncode, out = _verify_command(
            project,
            instrumented_script(clauses, stop_on_failure=True) if marked else cmd,
            cwd, VERIFY_TIMEOUT if timeout is None else timeout)
    except subprocess.TimeoutExpired as expired:
        per_clause, _, cleaned = parse_clause_output(expired.output or "")
        report = timeout_failure_report(cmd, clauses if marked else None,
                                        per_clause, cleaned, expired.timeout)
        index = max(per_clause, default=1)
        return False, _verify_failure(report, cmd, clauses if marked else None,
                                      index, None, per_clause.get(index, cleaned))
    per_clause, failed, cleaned = parse_clause_output(out)
    if returncode == 0:
        vacuous = vacuous_green_report(cmd, cleaned)
        if vacuous:
            index = next((n for n, text in per_clause.items()
                          if VACUOUS_RE.search(text)), 1)
            return False, _verify_failure(vacuous, cmd, clauses, index, 0,
                                          per_clause.get(index, cleaned))
        return True, cleaned.strip()[-2000:]
    report = failure_report(cmd, clauses if marked else None,
                            per_clause, failed, returncode, cleaned)
    index = failed[0] if failed else max(per_clause, default=1)
    return False, _verify_failure(report, cmd, clauses if marked else None,
                                  index, returncode, per_clause.get(index, cleaned))


def _verify_failure(report, cmd, clauses, index, status, output):
    command = clauses[index - 1] if clauses and 1 <= index <= len(clauses) else cmd
    facts = {'command_index': index, 'command': command,
             'exit_status': status,
             'last_output_line': output.splitlines()[-1] if output else '(no output)'}
    return VerificationOutput(report, [], failure=facts)


class RunFailure(Exception):
    failure_kind = 'unclassified'

    def __init__(self, reason, failure_kind=None):
        from store.enums import FailureKind

        super().__init__(reason)
        self.reason = reason
        self.failure_kind = FailureKind(failure_kind or getattr(
            reason, 'failure_kind', self.failure_kind)).value


class InfraFailure(RunFailure):
    """Says nothing about the ticket, so it spends none of its attempts."""

    failure_kind = 'infra'


class MergeLockHeld(InfraFailure):
    failure_kind = 'merge_lock'


class MergeParked(Exception):
    """Not a failure: the run is parked, neither released nor counted."""


def outcome_class_of(exc):
    return "infra" if isinstance(exc, InfraFailure) else "work"


def sh(args, cwd=None, env=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"`{args}` failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


# A file rather than an flock, so the supervisor can see and clear a dead holder.
MERGE_LOCK_WAIT_SEC = 180
MERGE_LOCK_POLL_SEC = 1.0


def merge_lock_path(project):
    """Never in the repository, where a task's `git add -A` could commit it."""
    return project.holo_dir / "merge.lock"


def read_merge_lock(path):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    parts = text.split()
    run_id = int(parts[0]) if parts and parts[0].isdigit() else None
    try:
        taken_at = float(parts[1]) if len(parts) > 1 else None
    except ValueError:
        taken_at = None
    return run_id, taken_at


@contextlib.contextmanager
def merge_lock(project, run_id, wait=None, poll=None, on_wait=None,
               extend_wait=None, operation="gate"):
    from holophyte.loop.merge_lock import lock_nap

    wait = MERGE_LOCK_WAIT_SEC if wait is None else wait
    poll = MERGE_LOCK_POLL_SEC if poll is None else poll
    path = merge_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = f"{run_id if run_id is not None else '-'} {time():.3f}\n"
    started = monotonic()
    while True:
        try:
            with merge_lock_arbiter(path):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                # The flock marks the holder alive whatever the store says of
                # its run; taken under the arbiter, before any sweep can open.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(fd, stamp.encode())
        except FileExistsError:
            nap = lock_nap(path, monotonic() - started, wait, poll,
                           extend_wait, operation)
            if on_wait is not None:
                on_wait()
            sleep(nap)
            continue
        break
    try:
        yield path
    finally:
        try:
            if path.read_text() == stamp:
                path.unlink()
        except FileNotFoundError:
            pass
        os.close(fd)


@contextlib.contextmanager
def merge_lock_arbiter(path):
    """The arbiter is never unlinked: unlinking a flock file lets two hold it."""
    fd = os.open(path.with_name(f"{path.name}.arbiter"),
                 os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def remove_dead_merge_lock(path):
    """`removed`, `in_use` (its flock is held) or `gone`, judged under the arbiter."""
    with merge_lock_arbiter(path):
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return "gone"
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return "in_use"
            try:
                os.unlink(path)
            except FileNotFoundError:
                return "gone"
            return "removed"
        finally:
            os.close(fd)


class VerificationOutput(str):
    def __new__(cls, output, results, *, failure=None):
        value = super().__new__(cls, output)
        value.results = results
        value.failure = failure
        return value


def run_baseline(project, wt, tier, conn=None, run_id=None):
    from holophyte.config.config_tables import verify_config

    config = verify_config(project)
    if tier not in ("always", "before_merge"):
        raise ValueError(f"unknown verify tier: {tier}")
    results, reports = [], []
    ok = True
    failure = None
    for command in getattr(config, tier):
        ok, out = run_verify(command, wt, timeout=config.timeout_sec,
                             conn=conn, run_id=run_id, project=project)
        results.append({"source": "baseline", "tier": tier,
                        "command": command, "exitCode": 0 if ok else 1,
                        "output": str(out)})
        reports.append(f"[baseline:{tier}] {command}\n{out}")
        if not ok:
            failure = getattr(out, 'failure', None)
            break
    return ok, VerificationOutput("\n".join(reports), results, failure=failure)


def with_baseline(project, wt, command, ok, out, conn=None, run_id=None,
                  *, before_merge=False):
    import json

    import store

    results = ([{"source": "ticket", "command": command,
                 "exitCode": 0 if ok else 1, "output": str(out)}]
               if command else [])
    failure = getattr(out, "failure", None)
    reports = [str(out)]
    for tier in (("always", "before_merge") if before_merge else ("always",)):
        if not ok:
            break
        ok, baseline = run_baseline(project, wt, tier, conn, run_id)
        failure = baseline.failure
        results.extend(baseline.results)
        if baseline:
            reports.append(str(baseline))
    has_baseline = any(row["source"] == "baseline" for row in results)
    if has_baseline and conn is not None and run_id is not None:
        store.record_event(conn, run_id, "verification",
                           "Mechanical verification " + ("passed" if ok else "failed"),
                           level="detail",
                           payload=json.dumps({"verificationResults": results}))
    output = VerificationOutput("\n".join(reports), results, failure=failure)
    if before_merge:
        record_unreviewed_verification(conn, run_id, output)
    return ok, output


def record_unreviewed_verification(conn, run_id, output):
    import json

    from holophyte.redact import redact_document

    raw_results = getattr(output, "results", [])
    has_baseline = any(row["source"] == "baseline" for row in raw_results)
    results = redact_document(raw_results)
    if conn is None or run_id is None or not has_baseline:
        return
    with conn:
        row = conn.execute(
            "SELECT id, verificationResults FROM reviewRounds WHERE runId = ?"
            " ORDER BY round DESC LIMIT 1", (run_id,)).fetchone()
        if row:
            conn.execute("UPDATE reviewRounds SET verificationResults = ? WHERE id = ?",
                         (json.dumps(json.loads(row[1]) + results), row[0]))
