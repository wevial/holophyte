"""The verify gate: a ticket's command in, a red or green report out.

Task-line parsing, the `&&`-clause instrumentation that makes a failure
attributable, the four report builders, the process-group cap the gate and
the agent dispatch both run under, `run_verify` itself, and the two failure
classes the loop's close-out reads. Pure: strings in, report out, one
subprocess call. Nothing here knows the loop, the store or the target; the
one constant it shares with worktree setup, `VERIFY_TIMEOUT`, stays in
`holophyte.config`, which is the default `setup_timeout_sec` falls back to.

Second slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import os
import re
import signal
import subprocess
from pathlib import Path

import ticket_template
from holophyte.config import VERIFY_TIMEOUT

DEFAULT_BUDGET_MIN = 20  # per-task wall-clock cap unless the line says "(N min)"


TASK_RE = re.compile(r"^[-*] \[ \] (.+)$", re.M)
BUDGET_RE = re.compile(r"\((\d+)\s*min\)\s*$")
VERIFY_RE = re.compile(r"\(verify:\s*(.+?)\)\s*$")


def parse_task(line):
    """Split 'do thing (verify: cmd) (15 min)' -> text, verify cmd, budget."""
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


# Markers the instrumented verify script prints around each `&&` clause, so a
# failure can be attributed to the clause that produced it.
CLAUSE_MARK = "__holo2_verify_clause__"
FAIL_MARK = "__holo2_verify_failed__"

# Zero-test summaries a runner prints while still exiting 0 — a gate that went
# green having verified nothing. Deliberately narrow: only the standard
# unittest and pytest phrasings, anchored to their own line, with no
# natural-language inference about other runners.
VACUOUS_RE = re.compile(r"^\s*(?:Ran 0 tests\b|collected 0 items\b)", re.M)


def split_and_clauses(cmd):  # noqa: C901 -- hand-written tokenizer; slice 4b owns it
    """Split a verify command on its top-level `&&` operators.

    Returns the clause list, or None when the command uses shell constructs
    whose meaning per-clause instrumentation could change (`||`, `;`, `&`,
    newlines, heredocs, backticks) or whose quoting/nesting is unbalanced.
    Those commands are run verbatim instead."""
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
            # A comment runs to end of line, so any `&&` inside it is text.
            # Only a trailing comment is safe to keep: a newline would resume
            # code, and inside `(...)` the comment would swallow the closer.
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
            if ch == "&" and prev not in "><":  # background job
                return None
            if cmd[i:i + 2] == "||" or ch in ";\n":
                return None
        buf.append(ch)
        i += 1
    if quote or escaped or depth:
        return None
    clauses.append("".join(buf).strip())
    return None if any(not c for c in clauses) else clauses


def instrumented_script(clauses):
    """One shell script that runs the clauses in order, stopping at the first
    failure with the original exit status. The clauses stay in a single shell,
    so `cd` and exported variables still carry across them.

    The failure is reported from an EXIT trap reading a clause counter, so a
    clause that ends the shell itself (`exit 7`) is still attributed rather
    than escaping as a bare non-zero status."""
    parts = ["__holo2_clause=0",
             "trap '__holo2_rc=$?; [ \"$__holo2_rc\" -eq 0 ] || "
             "printf \"%s\\n\" \"{} $__holo2_clause $__holo2_rc\"' EXIT"
             .format(FAIL_MARK)]
    for idx, clause in enumerate(clauses, 1):
        parts.append("__holo2_clause={}".format(idx))
        parts.append("printf '%s\\n' '{} {}'".format(CLAUSE_MARK, idx))
        parts.append("{{ {}\n}} || exit $?".format(clause))
    return "\n".join(parts)


def parse_clause_output(output):
    """Split marked output into per-clause text. Returns
    (per_clause, failed, cleaned) where failed is (clause index, exit code).

    A clause whose output has no trailing newline glues the next marker onto
    its last line ("first__holo2_verify_clause__ 2"), so markers are split
    off mid-line: the prefix stays with the clause that printed it."""
    per_clause, failed, cleaned, current = {}, None, [], None

    def emit(text):
        cleaned.append(text)
        if current is not None:
            per_clause[current].append(text)

    for line in output.splitlines():
        # Peel off any output a marker got glued onto.
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
    """Name the command that failed and show its output — never a bare
    non-zero exit. Silence is reported as silence, not as an empty pass.

    For a chain, every clause that actually ran is listed with its own
    output, and the clauses the failure short-circuited are named as not
    executed, so the reader can tell "did not run" from "ran and said
    nothing"."""
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


def timeout_failure_report(cmd, clauses, per_clause, cleaned, timeout):
    """Name the cap a command ran past and, for a marked chain, the clause
    that was running when it fired. Same shape as `failure_report()`, so a
    hung verify reads like any other failed verify: the earlier clauses are
    listed with their output, the running one is marked as timed out, and
    the ones the cap short-circuited are named as not executed.

    The running clause is the last one that announced itself: the chain
    stops at the first failure, so the highest marker seen is the one that
    never finished."""
    running = max(per_clause) if per_clause else None
    head = f"[verify] FAILED: verify timed out after {timeout:g}s"
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
    """A test command that exits 0 having collected no tests verified nothing,
    so it is RED. Returns the report naming `vacuous-green`, quoting the
    summary line that gave it away and the output around it, or None when the
    output shows tests actually ran."""
    m = VACUOUS_RE.search(cleaned)
    if not m:
        return None
    summary = cleaned[m.start():].splitlines()[0].strip()
    body = cleaned.strip() or "(no output)"
    return (f"[verify] FAILED: vacuous-green — exited 0 but ran no tests\n"
            f"[verify]   zero-test summary: {summary}\n"
            f"[verify]   full command: {cmd}\n"
            f"[verify]   output:\n{body[-2000:]}")


def contract_report(contracts, cwd):
    """Run the ticket's literal contract checks — each a (relative path,
    expected literal) pair parsed from its `## Contract checks` fence — against
    the worktree.

    Returns the report for the first declaration that does not hold, naming the
    path and the expected literal so a drifted value (KO-106's port) is
    actionable, or None when every declared literal is present. The comparison
    is a verbatim substring test: no globs, no regex, no shell.

    The checked file's contents are never echoed. A ticket may point a
    declaration at a configuration file holding credentials, and this report is
    forwarded to the reviewer; the path and the missing literal say what
    drifted without logging a secret.
    """
    root = Path(cwd)
    for path, literal in contracts or ():
        problem = ticket_template.contract_path_problem(path)
        if problem is None and not literal:
            problem = "declaration has an empty expected literal"
        target = root / path
        if problem is None and not target.is_file():
            problem = "declared file does not exist"
        if problem is None:
            if literal in target.read_text(errors="replace"):
                continue
            problem = "expected literal is absent from the file"
        return (f"[verify] FAILED: contract check — {problem}\n"
                f"[verify]   path: {path}\n"
                f"[verify]   expected literal: {literal}")
    return None


REAP_GRACE = 10       # how long the cap waits for a killed tree's last output


def reap_group(proc, expired):
    """Kill `proc`'s whole process group and return what it printed.

    `SIGKILL`, not a term-then-kill escalation: a command that ran past its
    cap has already had every chance to finish, and the caller's next move is
    to throw away the directory it was running in, so a graceful shutdown has
    nothing to save.

    A grandchild that put itself in a session of its own is outside the group
    and can keep the output pipe open after the group is gone, so the wait for
    the last output is itself capped -- reporting a timeout must not be a
    second way to hang. That fallback keeps the partial output the cap already
    captured and gives up on the trailing bytes such a process was still
    writing.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        out, _ = proc.communicate(timeout=REAP_GRACE)
    except subprocess.TimeoutExpired:
        # CPython attaches the partial output as `bytes` even under
        # `text=True`; hand back the text the caller was promised.
        out = expired.output or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
    return out


def run_capped(cmd, cwd, timeout):
    """Run one command under a hard cap. Returns `(returncode, output)`,
    or raises `subprocess.TimeoutExpired` carrying whatever it printed first.

    `cmd` is a shell string (a ticket's verify command, a setup command) or an
    argv list (an agent dispatch, where the prompt is data and must never
    reach a shell); either way the tree underneath it runs as one group.

    The process group is the point. `subprocess.run(timeout=...)` signals the
    shell it started and nothing underneath it, so a `make` that reached the
    cap is reported as over while its compilers keep running -- writing into a
    worktree the caller is about to delete, against caches the next round
    reads, with no handle left to stop them by. Starting the command in a
    session of its own makes the tree one killable unit, so the cap can end
    the command it timed rather than just the shell that spawned it.
    """
    with subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=str(cwd),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True) as proc:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            raise subprocess.TimeoutExpired(
                cmd, timeout, output=reap_group(proc, expired)) from None
        return proc.returncode, out


def run_verify(cmd, cwd, contracts=None, timeout=None):
    """Mechanical acceptance check. Returns (ok, output). Runs via shell on
    purpose: the command is author-supplied on the ticket, not agent output.

    A failure is always attributable: a top-level `&&` chain is marked clause
    by clause inside one shell, and the report points at the clause that
    exited non-zero, including when that clause failed without printing
    anything. An exit-0 run that reports zero collected tests is failed as
    `vacuous-green` rather than passed. A command that runs past
    the cap -- `timeout`, or `VERIFY_TIMEOUT` when the caller names none --
    is RED too, naming the cap and the clause that was running; it never
    raises `TimeoutExpired` at the caller.

    Literal contract checks declared on the ticket run first: they are
    deterministic, need no subprocess, and a drifted literal is a RED result
    even when the command itself passes. A ticket declaring none is unaffected.
    """
    drifted = contract_report(contracts, cwd)
    if drifted:
        return False, drifted
    passed = (f"[verify] contract checks passed: {len(contracts)}\n"
              if contracts else "")
    if not cmd:
        return True, passed + "(no verify command)"
    clauses = split_and_clauses(cmd)
    marked = bool(clauses) and len(clauses) > 1
    try:
        returncode, out = run_capped(
            instrumented_script(clauses) if marked else cmd,
            cwd, VERIFY_TIMEOUT if timeout is None else timeout)
    except subprocess.TimeoutExpired as expired:
        # The cap is a failed verify, not a crash: `run_capped` has already
        # reaped the process group, and what the command printed before the
        # kill says which clause was running when it fired.
        per_clause, _, cleaned = parse_clause_output(expired.output or "")
        return False, timeout_failure_report(cmd, clauses if marked else None,
                                             per_clause, cleaned,
                                             expired.timeout)
    per_clause, failed, cleaned = parse_clause_output(out)
    if returncode == 0:
        vacuous = vacuous_green_report(cmd, cleaned)
        return (False, vacuous) if vacuous else (True, passed + cleaned.strip()[-2000:])
    return False, failure_report(cmd, clauses if marked else None,
                                 per_clause, failed, returncode, cleaned)


class RunFailure(Exception):
    """A run failing on purpose: the message is the close-out reason.

    Raised inside `run_task()` where the code knows *why* the run cannot
    continue, and caught in `main()` beside the crash handler — the
    difference is only the log line; both end as the same failed run with
    the text as its `outcomeReason`.
    """


class InfraFailure(RunFailure):
    """A run failing for a reason that says nothing about the ticket.

    The factory's own plumbing gave out — a reviewer container that would not
    start, a route that did not answer — or the run ended before any work
    began. Caught exactly where `RunFailure` is and closed out the same way,
    with one difference: the row is written with `outcomeClass = 'infra'`, and
    `failure_history()` leaves it out of the count that parks a ticket for a
    human. A Docker outage is not evidence about the ticket, and two of them
    must not spend its attempts (holophyte-bugs #4 and #6: a spurious
    post-claim failure was KO-150's second strike).

    A subclass rather than a flag on the message so every existing raise site
    keeps its meaning: nothing that raises `RunFailure` today is reclassified.
    """


class MergeParked(RunFailure):
    """An approved, verified candidate parked for a human to say "merge".

    Raised at the merge gate under `[merge] approve = "human"`, after the
    review approved and the pre-merge verify passed, so the run ends the way
    a refused merge ends -- lease released, branch and worktree preserved --
    except that nothing went wrong. The message is the close-out reason.

    Classed `infra` on the row for the one reason that class exists: the
    escalation count (`failure_history()`) leaves it out, so a park is never
    one of the strikes that blocks a ticket. A class of its own (`parked`)
    is what the row should say; `runs.outcomeClass` carries a CHECK that a
    migrated store cannot widen without a table rebuild, which is a schema
    ticket rather than this one. Its ticket is `blocked_on_operator` with
    the question `merge?`, which is where `/attention` and the operator read
    what the run is waiting for.
    """


def outcome_class_of(exc):
    """The `runs.outcomeClass` a failure that ended in `exc` is written with."""
    return "infra" if isinstance(exc, (InfraFailure, MergeParked)) else "work"


def sh(args, cwd=None):
    """Run an argv list — no shell, so task text can't break quoting."""
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"`{args}` failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()
