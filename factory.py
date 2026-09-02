#!/usr/bin/env python3
"""holo2: a minimal software factory.

Loop:
  0. Open the v2 store (WAL-mode SQLite, a sibling file of the target repo).
  1. Claim the first ready ticket from Linear (project in linear_provider),
     mirror it into the store and take the project's run lease before any
     branch exists; the lease goes back when the run ends, merged or not.
  2. Spawn an implementer agent (goal-based) on a branch.
  3. Spawn a read-only reviewer agent on the committed result.
  4. If findings: implementer fixes, one narrow re-review, and a fix round for
     its findings too. Max 2 review rounds.
  5. If neither round approved: one terminal adjudication run — PASS or FAIL on
     the final state, no new findings, no further fixes.
  6. On approval or a terminal PASS: merge to main, check off the task, repeat.

`--report` runs none of the above: it prints the store's estimate-vs-actual
table for the target and exits, so the timing the runs recorded is a query
rather than a grep over FINDINGS.md. `--sweep` runs none of it either: it
reads the live runs and says which have tripped a mechanical condition -- a
dead heartbeat or a blown time box -- without touching one. `--sweep --act`
adds the acting: each tripped run is failed and its leases released through
the same close-out step 6's failures take, so a crashed run stops holding the
queue. Its branch and worktree are left where they are, for a human.
`--supervise` is that acting sweep on a timer: one long-lived process per
target, held to one by a lockfile, sweeping every minute until it is told to
stop.
"""
import argparse
import collections
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from time import monotonic, time

import review_runner
import store
import ticket_template

MAX_ROUNDS = 2
DEFAULT_BUDGET_MIN = 20  # per-task wall-clock cap unless the line says "(N min)"
DEFAULT_TARGET = Path("/srv/dev/holo2test")
# The paths a run works against, plus the config they carry. They are set by
# `retarget()` below rather than written out here, so the derivation lives in
# one place and the command line is the only thing that chooses a target:
# importing this module used to read `sys.argv[1]`, which made every
# `python3 -m unittest discover` retarget the factory at a directory called
# "discover".
TARGET = HOLO_DIR = STORE_PATH = WORKTREES = CONFIG_PATH = None
# The parsed `HOLOPHYTE_HOME/SLUG/config.toml`, cached by `config()`; None until it has
# been read. `{}` is the documented normal case once it has: every knob the
# file can set has a hardcoded default, so an absent file is exactly today's
# behavior.
CONFIG = None


def load_config(path):
    """Parse the target's TOML config, or `{}` when there is no file.

    An absent file is the common case and means "all defaults" — the factory
    ships no config of its own. A file that exists but does not parse is a
    startup error naming the file and what `tomllib` objected to: a config the
    operator wrote and the factory silently ignored would route a run to a
    harness nobody chose, which is the one outcome the file exists to prevent.
    Unknown tables are left alone, so a config written for a later version
    still loads here; a key this version does not read inside a table it does
    is refused by `check_config_keys()` at startup.
    """
    path = Path(path)
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"[holo2] malformed config {path}: {exc}") from exc


DEFAULT_HOLOPHYTE_HOME = "~/.holophyte"
# The sidecars SQLite keeps beside a WAL-mode database. They are part of the
# store, so a move that left them behind would move a truncated history.
STORE_SIDECARS = ("-wal", "-shm")


def state_dir(target):
    """Where everything the factory knows about `target` lives.

    `HOLOPHYTE_HOME/<basename>-<hash>`, defaulting to `~/.holophyte`. Host
    state, not repo state: what is kept here is this host's agent routes,
    leases and heartbeats, so it belongs to the host rather than to a
    checkout that gets cloned, moved and deleted. One home also gives
    `--serve` and the drawer a single place to enumerate a host's targets,
    leaves project parents such as `/srv/dev` free of dotted artifacts, and
    works when that parent is not writable. The hash of the absolute path is
    what keeps `/a/repo` and `/b/repo` -- two repositories, two histories --
    out of each other's store.
    """
    target = Path(target)
    home = Path(os.environ.get("HOLOPHYTE_HOME") or DEFAULT_HOLOPHYTE_HOME)
    digest = hashlib.sha1(str(target.resolve()).encode()).hexdigest()[:8]
    return home.expanduser() / f"{target.name}-{digest}"


def legacy_state_layouts(target):
    """The pre-home addresses for `target`'s state, as moves into a new dir.

    Two of them ever existed: KO-165's `<target>.holophyte/` directory, and
    before it a family of dotted siblings (`<target>.holophyte.db` with its
    WAL sidecars, `<target>.holophyte.toml`). Each layout is returned as
    `(directory_to_remove, [(source, name_in_state_dir), ...])`.
    """
    target = Path(target)
    stem = target.parent / f"{target.name}.holophyte"
    layouts = []
    if stem.is_dir():
        moves = [(path, path.name) for path in sorted(stem.iterdir())
                 if path.is_file()]
        if moves:
            layouts.append((stem, moves))
    moves = []
    for suffix in ("", *STORE_SIDECARS):
        sibling = stem.with_name(f"{stem.name}.db{suffix}")
        if sibling.is_file():
            moves.append((sibling, f"store.db{suffix}"))
    toml = stem.with_name(f"{stem.name}.toml")
    if toml.is_file():
        moves.append((toml, "config.toml"))
    if moves:
        layouts.append((None, moves))
    return layouts


def adopt_legacy_state(target, destination, out=None):
    """Move `target`'s legacy state into `destination`, once, loudly.

    KO-165 moved the store's address and shipped no migration with it: the
    next run on gembox opened an empty database at the new path and shadowed
    fifteen runs, the ticket's failure count, every intervention row and the
    `[agents] implementer` route the old config carried. Nothing was lost and
    nothing said so, which is the failure this function exists to make
    impossible -- either the history moves with the address, or the factory
    refuses to start against half of it.

    What makes it a one-time event is the store at the new address, not the
    directory holding it: an operator who writes `config.toml` at the new
    address first -- which the README tells them to do -- creates that
    directory without adopting anything, and gating on the directory would
    leave the legacy history for the empty store `open_store()` writes a
    moment later to shadow. So adoption runs whenever `destination` has no
    store, merging into the directory if it is already there, and a file
    already sitting at a landing address stops the whole move rather than
    being overwritten. Once the store has moved, whatever else is lying
    beside the checkout is somebody's backup, not this run's state.
    """
    out = sys.stdout if out is None else out
    destination = Path(destination)
    layouts = legacy_state_layouts(target)
    stores = [source for _, moves in layouts for source, name in moves
              if name == "store.db"]
    new_store = destination / "store.db"
    if len(stores) > 1 or (stores and new_store.exists()):
        standing = [str(new_store)] if new_store.exists() else []
        raise SystemExit(
            f"[holo2] {target} has more than one store: "
            + ", ".join(standing + [str(path) for path in stores])
            + "; refusing to start against one and shadow the rest -- move"
            " or remove all but the history you want to keep")
    if new_store.exists() or len(layouts) != 1:
        return []
    stem, moves = layouts[0]
    # Every landing address is checked before the first move, so a refusal
    # leaves both layouts whole rather than half of one in each place.
    for source, name in moves:
        landing = destination / name
        if landing.exists():
            raise SystemExit(
                f"[holo2] cannot adopt {source}: {landing} is already there;"
                " refusing to overwrite it -- move or remove one of the two")
    destination.mkdir(parents=True, exist_ok=True)
    adopted = []
    for source, name in moves:
        landing = destination / name
        try:
            os.replace(source, landing)
        except OSError:
            # A home on a different filesystem from the project parent is
            # ordinary; `os.replace` cannot cross that line and `shutil.move`
            # can.
            shutil.move(str(source), str(landing))
        print(f"[holo2] adopted {source} -> {landing}", file=out)
        adopted.append(landing)
    if stem is not None:
        with contextlib.suppress(OSError):
            stem.rmdir()
    return adopted


def retarget(target, adopt=True):
    """Point TARGET, the paths derived from it and CONFIG at `target`.

    Called once at import for the default and again by `cli()` for whatever
    the command line names; nothing else moves these, so a caller that wants a
    different target says so here instead of patching one path and leaving the
    other two pointing at the last one. The config is loaded here for the same
    reason: it is derived from the target, so it moves when the target does.

    `adopt=False` derives the paths and nothing else, which is what the
    import-time call for `DEFAULT_TARGET` uses. Adopting there would move
    some unrelated target's state as a side effect of importing this module,
    and -- where that target has two stores -- would make `import factory`
    and `factory.py --help` exit, the same rule `config()` follows: nothing
    target-specific happens before `cli()` has picked a target.
    """
    global TARGET, HOLO_DIR, STORE_PATH, WORKTREES, CONFIG_PATH, CONFIG
    TARGET = Path(target)
    # Everything the factory keeps about a target lives in one directory
    # under the host's home, `HOLOPHYTE_HOME/SLUG/`, created the first time
    # something has to write there. Not inside the target: the factory's own
    # .gitignore says nothing about the target checkout, so a store written
    # into TARGET would leave the database and its two WAL sidecars untracked
    # in whatever repo the loop is working on -- dirt a task's `git add -A`
    # could sweep into a commit. Not beside it either: see `state_dir()`.
    HOLO_DIR = state_dir(TARGET)
    # Whatever a previous layout left beside the checkout moves in here now,
    # before anything opens a store at the new address and finds it empty.
    if adopt:
        adopt_legacy_state(TARGET, HOLO_DIR)
    # The loop's durable state: one WAL-mode SQLite file per target repo.
    STORE_PATH = HOLO_DIR / "store.db"
    # Config for a target is not a file the target has to carry either.
    CONFIG_PATH = HOLO_DIR / "config.toml"
    # The worktree directory predates the state directory and is heavy git
    # state rather than factory state; it keeps its own sibling address.
    WORKTREES = TARGET.parent / f"{TARGET.name}.worktrees"
    # Dropped, not read: retargeting invalidates the cache, and `config()`
    # parses the new file the first time something asks for it.
    CONFIG = None


def config():
    """The target's parsed config, read once per target.

    Read on demand rather than by `retarget()`, which runs at import for the
    default target: parsing there made a malformed
    `~/.holophyte/holo2test-*/config.toml` an error for `--help`, for importing
    this module at all, and for a run pointed at some entirely different
    repository. Nothing that reads config runs before `cli()` picks a target,
    and `cli()` reads it as soon as it has one, so the file a run actually
    depends on is still parsed at startup: a malformed one aborts before a
    ticket is claimed, not in the middle of a round.
    """
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config(CONFIG_PATH)
    return CONFIG


# Paths only: see `retarget()`. The default target's state is adopted when
# `cli()` names it, not because somebody imported this module.
retarget(DEFAULT_TARGET, adopt=False)

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


def split_and_clauses(cmd):
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


VERIFY_TIMEOUT = 300  # per-command wall-clock cap, verify and worktree setup
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
    """Run one shell command under a hard cap. Returns `(returncode, output)`,
    or raises `subprocess.TimeoutExpired` carrying whatever it printed first.

    The process group is the point. `subprocess.run(timeout=...)` signals the
    shell it started and nothing underneath it, so a `make` that reached the
    cap is reported as over while its compilers keep running -- writing into a
    worktree the caller is about to delete, against caches the next round
    reads, with no handle left to stop them by. Starting the command in a
    session of its own makes the tree one killable unit, so the cap can end
    the command it timed rather than just the shell that spawned it.
    """
    with subprocess.Popen(cmd, shell=True, cwd=str(cwd),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True) as proc:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            raise subprocess.TimeoutExpired(
                cmd, timeout, output=reap_group(proc, expired)) from None
        return proc.returncode, out


def run_verify(cmd, cwd=None, contracts=None, timeout=None):
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
    drifted = contract_report(contracts, cwd or TARGET)
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
            cwd or TARGET, VERIFY_TIMEOUT if timeout is None else timeout)
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


def outcome_class_of(exc):
    """The `runs.outcomeClass` a failure that ended in `exc` is written with."""
    return "infra" if isinstance(exc, InfraFailure) else "work"


def sh(args, cwd=None):
    """Run an argv list — no shell, so task text can't break quoting."""
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"`{args}` failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


# Role -> harness/model pins. Each gate uses a distinct, live-probed route:
# Claude Code / Opus High implements; the local container boundary runs Codex /
# GPT-5.6 Sol Medium against a detached, zero-remote, read-only candidate.
# These are the defaults an absent `[agents]` table leaves in place, not
# assumptions: a target that names its own command for a role gets that one.
IMPL_MODEL = "opus"
IMPL_EFFORT = "high"
REVIEW_PROFILE = "codex-sol-medium"

# The loop's internal role names, and the `[agents]` key each one reads. The
# config speaks the job title an operator writes on a ticket; the loop speaks
# the verb it dispatches.
AGENT_CONFIG_KEYS = {
    "implement": "implementer",
    "review": "reviewer",
    "adjudicate": "adjudicator",
}

# Every key the factory reads, per table it reads. `check_config_keys()` holds
# a config to this at startup: a key inside one of these tables that is not
# listed here is a typo (`setup_timeout_min` for `setup_timeout_sec`), and a
# typo the factory ignored would leave the operator believing a knob is set
# that is not. Tables not named here are left alone -- a config written for a
# later version, or for another tool reading the same file, still loads.
# `[supervisor]`'s entry is filled in beside `SUPERVISOR_KEYS`, where those
# knobs and their defaults are defined.
KNOWN_KEYS = {
    "agents": frozenset(AGENT_CONFIG_KEYS.values()),
    "worktree": frozenset({"setup", "setup_timeout_sec"}),
}


def check_config_keys():
    """Refuse a key the factory does not read inside a table it does.

    Runs at startup for every mode, in the same breath as `sweep_config()`
    checks the `[supervisor]` values: an unknown key is the same kind of
    mistake as a value outside its constraint, and deserves the same loud
    answer while nothing is claimed. The message names the file, the table,
    the key and the keys the table does accept, so the operator can see the
    one they meant. A table that is not a table is left to the reader that
    owns it (`agent_command()`, `setup_commands()`, `sweep_config()`), which
    already says so in its own words.
    """
    for table, known in KNOWN_KEYS.items():
        section = config().get(table)
        if not isinstance(section, dict):
            continue
        for key in section:
            if key not in known:
                raise SystemExit(
                    f"[holo2] {CONFIG_PATH}: [{table}] {key}: unknown key; "
                    f"[{table}] accepts: {', '.join(sorted(known))}")


def agent_command(role, goal):
    """The configured argv for `role`, or None when the config names none.

    The goal is appended as the command's last argument, which is where both
    default harnesses take a prompt (`claude ... -p PROMPT`, `codex exec ...
    PROMPT`). Writing it as an argv element rather than interpolating it into
    a shell string is the same rule `sh()` follows: task text is data, and it
    never gets to break quoting.

    A key that is present but unusable — a non-string, or a string that splits
    to nothing — is a startup error rather than a fallback to the default: the
    operator asked for a route, and quietly running the built-in one instead
    would answer a different question than the one the config asked.
    """
    command = (config().get("agents") or {}).get(AGENT_CONFIG_KEYS[role])
    if command is None:
        return None
    if not isinstance(command, str):
        raise SystemExit(
            f"[holo2] {CONFIG_PATH}: [agents] {AGENT_CONFIG_KEYS[role]} must be "
            f"a command string, got {type(command).__name__}")
    argv = shlex.split(command)
    if not argv:
        raise SystemExit(
            f"[holo2] {CONFIG_PATH}: [agents] {AGENT_CONFIG_KEYS[role]} is empty")
    return argv + [goal]


def check_agent_commands():
    """Resolve every configured `[agents]` command before the loop claims work.

    Reading the config at startup only proved the file was TOML. The commands
    it named were first looked at when a round dispatched them, which is after
    a ticket is claimed, its branch cut and its worktree created: a typo in a
    program name or a stray quote in `reviewer` surfaced as a mid-run
    `FileNotFoundError`, with a run already in flight and its lease held. The
    same mistakes are caught here, before anything is claimed, where the only
    cost of being wrong is an error message.

    The check parses through `agent_command()` rather than re-reading the
    table, so a string this refuses is exactly a string a round would have
    refused, and one it accepts splits at startup into the argv the round will
    dispatch -- no second, kinder parser to disagree with the real one.

    What it can settle here is the program: it has to resolve, on this PATH,
    to a file that is executable. What it deliberately does not do is run it.
    A configured route is an agent turn; probing it live would dispatch a real
    one, against no ticket, on every startup.

    A relative program path with a directory in it (`./review.sh`) is refused
    rather than guessed at. Rounds run with `cwd` set to a task worktree that
    does not exist yet, so that name resolves somewhere this check cannot look
    and the operator has not named. An absolute path or a PATH lookup says
    where it means.
    """
    for role, key in AGENT_CONFIG_KEYS.items():
        argv = agent_command(role, "")
        if argv is None:
            continue
        program = argv[0]
        if os.path.dirname(program) and not os.path.isabs(program):
            raise SystemExit(
                f"[holo2] {CONFIG_PATH}: [agents] {key}: relative command path "
                f"{program!r} -- rounds run in a task worktree, so name the "
                f"program by an absolute path or leave it to PATH")
        if shutil.which(program) is None:
            raise SystemExit(
                f"[holo2] {CONFIG_PATH}: [agents] {key}: no executable "
                f"{program!r} on PATH")


def agent_route(role):
    """What ran `role`'s turn, named for the record the round leaves.

    The default reviewer profile, or the configured command when the target
    named one. A `reviewRounds` row reading `codex-sol-medium` about a round
    some other harness ran would be evidence of something that did not happen,
    and the rows are what FINDINGS.md and the fingerprint are built from.
    """
    return ((config().get("agents") or {}).get(AGENT_CONFIG_KEYS[role])
            or REVIEW_PROFILE)


def publish_review_refs(repo, base_sha, candidate_sha):
    """Name this round's two commits `refs/review/base` and
    `refs/review/candidate` inside `repo`.

    The default reviewer route gets those two refs from
    `review_runner.stage_candidate()`, which creates them in the checkout it
    builds. A configured reviewer or adjudicator runs in the task worktree
    instead, where nothing had ever created them — and the prompt it is handed
    tells it to review the base and the candidate by exactly those names. So
    the worktree gets the same two names for the same two commits, and the
    override is asked about the frozen pair rather than about whatever HEAD
    happens to be.

    The exact-SHA requirement holds on this route too, the same way the staged
    one enforces it: each side must be a full commit SHA that resolves here to
    itself, and the base must be an ancestor of the candidate. A round argues
    about one named candidate against one named base, whoever runs it.

    The refs live in the target repository's ref store, shared by its
    worktrees; that is safe because the project's run lease single-threads
    runs, and each round overwrites both refs with its own pair before
    dispatching. They are left behind afterwards, like the branch a finished
    run leaves for a human to look at.
    """
    for sha in (base_sha, candidate_sha):
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
            cwd=repo, capture_output=True, text=True)
        if resolved.returncode or resolved.stdout.strip() != sha:
            raise review_runner.ReviewBoundaryError(
                f"not a full commit SHA in {repo}: {sha}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", base_sha,
                       candidate_sha], cwd=repo).returncode:
        raise review_runner.ReviewBoundaryError(
            f"base {base_sha} is not an ancestor of {candidate_sha}")
    for name, sha in (("base", base_sha), ("candidate", candidate_sha)):
        sh(["git", "update-ref", f"refs/review/{name}", sha], cwd=repo)


def agent(role, goal, cwd, *, base_sha=None, candidate_sha=None):
    """Run one agent turn for a role. Returns combined output text.

    `adjudicate` is the terminal pass/fail round. It takes the same
    independent reviewer route as `review` — a fresh dispatch that knows only
    the diff and the ticket — but its verdict is not enforced at the boundary:
    a reply that names no clean verdict has to reach the loop as text so it
    can be recorded and read as FAIL.

    A configured command replaces the role's route, so an `[agents] reviewer`
    override is also an opt-out of the hardened container the default reviewer
    runs behind. What it does not opt out of is the pair the round is about:
    the exact-SHA requirement is enforced either way, and either way the two
    commits reach the reviewer as `refs/review/base` and
    `refs/review/candidate` — the names its prompt uses — the staged checkout
    on the default route, the task worktree on the configured one.
    """
    if role not in AGENT_CONFIG_KEYS:
        raise ValueError(role)
    if role in ("review", "adjudicate") and not (base_sha and candidate_sha):
        raise ValueError(f"{role} requires exact base_sha and candidate_sha")
    cmd = agent_command(role, goal)
    if cmd is None:
        if role != "implement":
            try:
                return review_runner.run_review(
                    repo=Path(cwd),
                    base_sha=base_sha,
                    candidate_sha=candidate_sha,
                    prompt=goal,
                    profile=REVIEW_PROFILE,
                    timeout=1800,
                    verdicts=(review_runner.REVIEW_VERDICTS
                              if role == "review" else None),
                )
            except review_runner.ReviewBoundaryError as e:
                # The runner could not stage, start or read the reviewer —
                # a missing CLI, an image that will not build, a container
                # that produced no events. The candidate was never judged,
                # so the failure is the factory's, not the ticket's.
                raise InfraFailure(f"reviewer route failed for {role}:"
                                   f" {e}") from e
        cmd = ["claude", "-p", goal, "--model", IMPL_MODEL,
               "--effort", IMPL_EFFORT]
    elif role != "implement":
        publish_review_refs(Path(cwd), base_sha, candidate_sha)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=1800)
    return (r.stdout + "\n" + r.stderr).strip()


# Agent replies reach the ledger as raw terminal output — ANSI-coloured tool
# traces — and as reviewer prose that heads its own sections. FINDINGS.md is
# append-only evidence, so whatever lands there is permanent: sanitize at the
# append boundary rather than cleaning the file up afterwards.
# A CSI sequence is introduced either by ESC-[ or by the single C1 byte \x9b;
# matching only the first left `\x9b31m` to lose its introducer and print as
# literal `31m`.
ANSI_CSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-9;?]*[ -/]*[@-~]")
# C0 and C1 controls, minus \t and \n.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Both Markdown heading forms, which the outline treats identically: ATX
# (`## Blockers`, indentable by up to three spaces) and Setext (a paragraph
# line underlined by `===` or `---`). The Setext lookahead skips a line that
# is itself an ATX heading or a list/quote marker, leaving those to ATX_RE
# and to the thematic break they actually are.
SETEXT_RE = re.compile(r"^ {0,3}(?![-*+>#\s])(.+?)[ \t]*\n {0,3}(?:=+|-+)[ \t]*$", re.M)
ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$", re.M)
# A real verdict is one short line — `VERDICT: REQUEST_CHANGES` is 24 chars.
# The line truncation is obliged to keep is agent-written, though, and a
# malformed reply is persisted verbatim, so cap it: otherwise `VERDICT: ` plus
# 10k characters is one trailing "verdict" line that carries the whole record
# past its budget.
MAX_VERDICT_CHARS = 200
TRUNCATION_MARKER = "[… truncated]"


# --- worktree setup ----------------------------------------------------------
# The second table a target can write. `[worktree] setup` is the list of shell
# commands a freshly cut task worktree needs before an agent works in it: the
# venv, module download or generated file the target's toolchain would
# otherwise borrow from the main checkout, quietly, and get wrong the moment a
# task changes a dependency. An absent table is today's behavior -- nothing
# runs, and a run costs exactly what it costs now.


def setup_commands():
    """The target's `[worktree] setup` list, or `[]` when it names none.

    Each entry is one shell command, run in order. A table that is present but
    unusable -- not a list, an entry that is not a string, an entry that is
    blank -- is an error rather than a skipped step, for the reason
    `agent_command()` refuses a bad `[agents]` row: a setup command the
    operator wrote and the loop silently dropped would hand the implementer a
    worktree nobody prepared, and that surfaces far away from the config, as a
    toolchain failure in the middle of a round.
    """
    commands = (config().get("worktree") or {}).get("setup")
    if commands is None:
        return []
    if not isinstance(commands, list):
        raise SystemExit(
            f"[holo2] {CONFIG_PATH}: [worktree] setup must be a list of "
            f"command strings, got {type(commands).__name__}")
    for command in commands:
        if not isinstance(command, str):
            raise SystemExit(
                f"[holo2] {CONFIG_PATH}: [worktree] setup: every entry must be "
                f"a command string, got {type(command).__name__}")
        if not command.strip():
            raise SystemExit(
                f"[holo2] {CONFIG_PATH}: [worktree] setup: entry {command!r} "
                "is empty")
    return commands


def setup_timeout():
    """The per-command cap on `[worktree] setup`, in seconds.

    `[worktree] setup_timeout_sec` when the target names one, else the same
    `VERIFY_TIMEOUT` a verify command gets: setup is a build step, and a Go
    module download or a fat pip install legitimately needs more patience
    than stdlib Python's nothing. The value is held to the constraint
    `sweep_config()` holds an interval to -- a finite positive number, with
    booleans refused as numbers -- and a value outside it is a startup error
    naming the key, for the reason a bad `[supervisor]` value is: a cap the
    factory quietly replaced with its default would bound the setup with a
    number nobody chose.
    """
    value = (config().get("worktree") or {}).get("setup_timeout_sec")
    if value is None:
        return VERIFY_TIMEOUT
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise SystemExit(
            f"[holo2] {CONFIG_PATH}: [worktree] setup_timeout_sec must be a "
            f"finite positive number of seconds, got {value!r}")
    return value


def check_worktree_setup():
    """Parse the `[worktree] setup` table before the loop claims work.

    `check_agent_commands()`'s sibling, here for the same reason: a table read
    for the first time inside a run would abandon a claimed ticket, a cut
    branch and a held project lease over something startup could have said in
    one sentence. It parses through `setup_commands()`, so a table this
    accepts is exactly a table a run would accept.

    What it deliberately does not settle is the commands themselves. They are
    shell, not argv -- `run_verify()` runs them the way it runs a ticket's
    verify command -- and they are written against a worktree that does not
    exist yet, so there is nothing here to resolve them against. Startup
    settles the shape of the table; the worktree settles the rest. The cap
    the commands run under is checked here too, for the same reason.
    """
    setup_commands()
    setup_timeout()


def timeout_report(cmd, expired):
    """Read one `subprocess.TimeoutExpired` as a failure report.

    A command that hangs is a failed command, not an unhandled exception: it
    is the cap doing its job, and the caller can only act on it if it arrives
    as the same `(ok, report)` a non-zero exit arrives as. Whatever the
    command printed before the cap fired is kept -- a hung build says where it
    hung in its last line of output -- and trimmed to the same 2000 characters
    a passing verify keeps, with silence reported as silence.
    """
    out = expired.output or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    out = out.strip()[-2000:]
    return (f"[verify] command timed out after {expired.timeout:g}s: {cmd}\n"
            + (out or "(no output before the timeout)"))


def run_worktree_setup(wt, conn=None, run_id=None):
    """Run the target's setup commands in the fresh worktree `wt`.

    Returns `(ok, report)`. Each command goes through `run_verify()`, so a
    failure reads like a failed verify and not like a bare non-zero exit: the
    command is named, its output is shown, a top-level `&&` chain is
    attributed clause by clause, and silence is reported as silence. The same
    machinery also means a wall-clock cap per command -- `setup_timeout()`,
    the target's `[worktree] setup_timeout_sec` over the verify cap; setup
    is a build step, not a round -- and the cap takes the command's whole
    process tree with it, so a build that hangs is not still writing into the
    worktree while the caller deletes it. A command that reaches the cap is
    failed like any other failing command rather than raised: on a `False`
    the caller discards a branch it cut fresh (a reused worktree, which may
    hold preserved work, is left in place), and a setup that hangs is exactly
    the case that must not leave a fresh cut behind.

    Commands run in order and stop at the first failure: step two of a setup
    assumes step one worked, so running on would only report a second failure
    about the first one. A target that names no setup runs nothing and records
    no phase, so an absent table leaves the run byte-identical to today's.
    """
    commands = setup_commands()
    if not commands:
        return True, ""
    timeout = setup_timeout()
    set_phase(conn, run_id, "working",
              f"worktree setup: {len(commands)} command(s) in {wt}")
    for n, command in enumerate(commands, 1):
        try:
            ok, out = run_verify(command, wt, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            ok, out = False, timeout_report(command, e)
        if not ok:
            return False, (f"[holo2] worktree setup command {n} of "
                           f"{len(commands)} FAILED: {command}\n{out}")
        print(f"[holo2] worktree setup {n}/{len(commands)} ok: {command}")
    return True, ""


def _trailing_verdict(text):
    """The `VERDICT:` line `text` ends on, which the record must keep.

    `review_runner.terminal_verdict` reads a verdict only in final position,
    so that is the one line truncation may never drop: it is the outcome the
    whole entry is evidence for. Any line opening `VERDICT:` counts, malformed
    ones included — those are exactly what a FAIL gets recorded from — and the
    line is cut to `MAX_VERDICT_CHARS` so that keeping it stays an exemption
    for one short line rather than a way around the entry budget.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[-1].startswith("VERDICT:"):
        return None
    verdict = lines[-1]
    if len(verdict) > MAX_VERDICT_CHARS:
        verdict = (verdict[:MAX_VERDICT_CHARS - len(TRUNCATION_MARKER)]
                   + TRUNCATION_MARKER)
    return verdict


def sanitize_findings(text, limit):
    """Make one agent-authored block safe to keep as a findings record.

    Strips ANSI escape sequences and other control bytes, demotes embedded
    Markdown headings to bold lines so the file's outline stays the factory's
    own `## <timestamp> — <ticket>` entries, and cuts an oversize block down
    with a visible marker. A trailing `VERDICT:` line survives all three: the
    escape and heading rules never match it, and truncation re-attaches it
    below the marker so an oversize entry still records its outcome.

    Applied at the row write (`finding_message`) rather than at a file
    append: FINDINGS.md is rendered from the rows now, so text a row carries
    dirty is text every later render carries dirty. `limit` is the calling
    boundary's own budget, and has no default — there is one boundary, and a
    sanitizer that guesses a bound is one that silently keeps the wrong one.
    """
    text = ANSI_CSI_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    text = SETEXT_RE.sub(r"**\1**", text)
    text = ATX_RE.sub(r"**\2**", text)
    if len(text) > limit:
        verdict = _trailing_verdict(text)
        tail = f"\n\n{TRUNCATION_MARKER}" + (f"\n\n{verdict}" if verdict else "")
        # max(): a negative bound would slice from the *end* and keep
        # nearly all of an oversize entry.
        head = text[:max(0, limit - len(tail))]
        if text[len(head)] != "\n" and "\n" in head:
            head = head[:head.rindex("\n")]  # never cut a line in half
        text = head.rstrip() + tail
    return text


def ledger(task_id, entry):
    """Archive one record as a Linear comment on the ticket.

    Nothing is appended to FINDINGS.md here any more: that file is rendered
    from the store's rows at close-out (`write_findings`), so it stays a
    bounded window instead of growing by one full transcript per turn. Linear
    comments are unchanged and stay the per-ticket archive of the whole prose
    — the store keeps the structure, Linear keeps the words.
    """
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        import linear_provider
        linear_provider.comment(task_id, f"**{ts}**\n\n{entry}")
    except Exception as e:
        print(f"[holo2] Linear comment failed ({e}); record kept in the store")


# --- review rounds as structured findings ------------------------------------
# The reviewer writes prose; `reviewRounds.findings` wants
# `{path, line?, severity, message}` objects, because a round the store holds
# as a paragraph cannot be compared with the next one. Extraction is therefore
# best-effort over the output format that exists today: what carries a file
# reference becomes a structured finding, an item the reviewer listed without
# one is kept under a placeholder path, and a reply that filed nothing at all
# is still recorded verbatim as a single finding. No complaint the reviewer
# filed is dropped — a round is evidence, and a lossy record of it would make
# the fingerprint agree about rounds that never matched.

# A path as a reviewer cites one: a token carrying a directory separator, or
# one whose extension is at least two characters, optionally followed by
# `:line`. Deliberately narrow — a bare word is prose, and a wrong path is
# worse than none, since the fingerprint keys on it. The two-character
# extension is what keeps `e.g.` and `i.e.` out of the findings; a one-letter
# extension still parses when the citation carries a directory (`src/a.c`).
# The lookbehind makes the match start at a token boundary, so a URL in the
# reviewer's prose is not read as the path `//linear.app`.
FINDING_PATH_RE = re.compile(
    r"(?<![\w:/.\-])"
    r"([\w.\-]*/[\w.\-/]*\.\w+|[\w.\-/]*[\w\-]\.[A-Za-z]\w+)(?::(\d+))?")
# An *explicit* severity marker, bracketed (`[P0]`, `(blocker)`) or opening the
# line (`- BLOCKER: ...`). Only a marker moves a finding off the default: tone
# is not severity, and a reviewer that sounds alarmed has not filed a p0.
SEVERITY_RE = re.compile(
    r"[\[(]\s*(p0|p1|p2|nit|blocker)\s*[\])]"
    r"|^[\s\-*\d.)]*(p0|p1|p2|nit|blocker)\b\s*[:\-]", re.I | re.M)
DEFAULT_SEVERITY = "p2"
# The path prefix a finding gets when the reviewer named none. Not a path any
# repository holds, so it cannot collide with a real file's findings, and
# readable in a `path:line:severity` key. A prefix rather than the whole path
# because that key is all §6 compares rounds by: findings sharing one
# placeholder would collapse into a single key, so a round complaining about
# `Dockerfile` and about `Makefile` would fingerprint as one complaint, and an
# unrelated round that filed one pathless p2 would fingerprint identically to
# it -- the false "same round twice" this task exists to make detectable.
# `unparsed_path()` appends a digest of the finding's own text to keep them
# apart.
UNPARSED_PATH = "(unparsed)"
# One finding is a complaint, not a transcript. Long enough for a blocker with
# its reasoning; short enough that a runaway reply cannot make one row the
# size of the review.
MAX_FINDING_CHARS = 2000
# A blank line, or the bullet/number that opens the next item: the boundaries
# a reviewer's findings list actually uses, so a finding keeps the lines that
# explain it instead of being cut to the one that names the file.
BLOCK_BREAK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def unparsed_path(message):
    """`UNPARSED_PATH` made distinct per complaint, by digesting its message.

    A finding with no path has only its text to say which complaint it is, so
    that is what the placeholder carries. This is not message prose leaking
    into the fingerprint generally -- a finding that cites a file still keys on
    the file -- it is the pathless case having nothing else to key on.

    Whitespace-normalized and lowercased before hashing, so a reviewer that
    rewraps or recapitalizes an unchanged complaint still keys to the same
    place. Prose it genuinely rewrote keys somewhere new, which is the
    direction to fail in: §6 reads a fingerprint match as a stuck review, so
    two distinct complaints reading as one is a false stop, while one complaint
    reworded reading as two only costs the softer overlap signal.
    """
    normalized = " ".join(message.split()).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{UNPARSED_PATH}:{digest}"


def finding_message(text):
    """One finding's message, safe to store: sanitized and bounded.

    The row write is where `sanitize_findings()` now applies, so a message
    carrying a terminal escape or a Markdown heading is cleaned once, here,
    rather than on every render of the window it will appear in.
    """
    return sanitize_findings(text, MAX_FINDING_CHARS).strip()


def raw_finding(reply):
    """The whole reply as one finding, for a round nothing parsed out of.

    The fallback the extraction is allowed to have: an unparseable reviewer
    reply is still a round that said something, and the alternative to keeping
    it under a placeholder path is a stored round that claims the reviewer
    found nothing.
    """
    message = finding_message(reply)
    return {"path": unparsed_path(message), "severity": DEFAULT_SEVERITY,
            "message": message}


def finding_blocks(text):
    """The reply split into candidate findings: bullet items and paragraphs."""
    blocks, current = [], []
    for line in text.splitlines():
        if not line.strip() or BLOCK_BREAK_RE.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line] if line.strip() else []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def parse_findings(reply):
    """Structured findings from one reviewer reply, best effort.

    Each block that cites a file becomes one finding: the cited path and line,
    an explicit severity marker if the block carries one, and the block itself
    as the message, so the reasoning stays with the complaint it belongs to.

    A block the path pattern found nothing in is still a finding when the
    reviewer wrote it as a list item, filed under an `unparsed_path()`
    placeholder with whatever severity it marked. The reviewer's own bullet is
    what says it filed a complaint, and this parser recognizing no path in it
    is a fact about the parser: `Dockerfile`, `Makefile` and a bare directory
    carry nothing to match on. Keeping only the items whose paths happen to
    parse would leave the round fingerprinted as a shorter complaint than the
    one that was made, and §6 compares those fingerprints. Prose around the
    list -- an opening sentence, the closing `VERDICT:` line -- is narration
    rather than a filed item and is not stored as one; a reply that filed no
    item at all still returns `raw_finding()` over the whole text, so a round
    is never recorded as having said nothing.
    """
    findings = []
    for block in finding_blocks(reply):
        match = FINDING_PATH_RE.search(block)
        listed = BLOCK_BREAK_RE.match(block) is not None
        if match is None and not listed:
            continue
        message = finding_message(block)
        if not message:
            continue  # a bullet with nothing after it is not a complaint
        path, line = ((match.group(1), match.group(2)) if match
                      else (unparsed_path(message), None))
        finding = {"path": path, "severity": finding_severity(block),
                   "message": message}
        if line and int(line) > 0:
            finding["line"] = int(line)
        findings.append(finding)
    return findings or [raw_finding(reply)]


def finding_severity(block):
    """`p2` unless the block carries an explicit severity marker."""
    match = SEVERITY_RE.search(block)
    if match is None:
        return DEFAULT_SEVERITY
    marker = (match.group(1) or match.group(2)).lower()
    return "p0" if marker == "blocker" else marker


def round_verdict(reply, verdicts):
    """The reply's verdict as `reviewRounds.verdict` spells it.

    Both reviewer vocabularies collapse onto §2's three: an approval or a
    terminal PASS is `pass`, findings or a terminal FAIL is
    `changes_requested`, and a reply with no clean verdict line is `error` —
    which is what the loop already reads a malformed adjudication as.
    """
    try:
        return {"APPROVE": "pass", "REQUEST_CHANGES": "changes_requested",
                "PASS": "pass", "FAIL": "changes_requested"}[
                    review_runner.terminal_verdict(reply, verdicts)]
    except review_runner.ReviewBoundaryError:
        return "error"


def reuse_leftover(wt, branch):
    """Ready leftover worktree `wt` for a new run on `branch`; (ok, reason).

    The reuse rule, stated once: preserved work survives. An unregistered
    directory is refused with a reason rather than deleted or crashed into
    (`git worktree add` onto a non-empty directory dies); uncommitted
    changes become a WIP commit on the branch; and the branch is reset to
    main only when the leftover verifiably holds nothing — a clean tree and
    a branch whose tips main already contains. A branch with preserved
    commits keeps them, with main merged in when it has moved on: the
    review routes and the merge both require main to be an ancestor of the
    candidate, so a carried branch predating the current main would
    otherwise stall the ticket on every rerun. A merge conflict, and a
    worktree sitting off the branch while the branch holds commits of its
    own, are a human's calls and are refused with the state named. Nothing
    is ever deleted here.
    """
    sh(["git", "worktree", "prune"], TARGET)
    r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                       cwd=TARGET, capture_output=True, text=True)
    # Exact resolved paths, not a substring test: slugs are truncated titles,
    # so a registered `.../add-a-thing-later` must not vouch for an
    # unregistered `.../add-a-thing` — and git prints resolved paths, so a
    # target reached through a symlink must not read as unregistered.
    registered = {str(Path(line[len("worktree "):]).resolve())
                  for line in r.stdout.splitlines()
                  if line.startswith("worktree ")}
    if str(Path(wt).resolve()) not in registered:
        return False, (f"leftover directory {wt} exists but is not a"
                       " registered worktree; a human moves it aside or"
                       " removes it before this ticket is run again")
    dirty = sh(["git", "status", "--porcelain"], cwd=wt)

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=wt, capture_output=True).returncode == 0

    # `checkout -B` below moves `branch` to wherever HEAD is, so a worktree
    # sitting off the branch — detached for a human's comparison, say —
    # while the branch holds commits of its own would silently orphan them.
    # Whose tip is the work is not this function's call to make.
    head_ref = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    branch_held = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=wt, capture_output=True).returncode == 0
    if head_ref != branch and branch_held and not is_ancestor(branch, "HEAD"):
        return False, (f"worktree {wt} is on {head_ref} while branch"
                       f" {branch} holds commits it does not; a human"
                       " reconciles them before this ticket is run again")
    if (not dirty and is_ancestor("HEAD", "main")
            and (not branch_held or is_ancestor(branch, "main"))):
        # Verifiably empty: a clean tree, and neither tip holding anything
        # main does not already have. The one case where resetting loses no
        # work — and the reset is what keeps the branch from starting behind
        # a main that moved on since the leftover was cut.
        sh(["git", "checkout", "-B", branch, "main"], cwd=wt)
        return True, ""
    # `-B` with no start point parks `branch` at the HEAD we are on without
    # touching the tree, so it cannot die on uncommitted files the way
    # `-B branch main` does.
    sh(["git", "checkout", "-B", branch], cwd=wt)
    if dirty:
        sh(["git", "add", "-A"], cwd=wt)
        # The identity is pinned so a target with no committer configured
        # cannot make the one function whose contract is "no traceback
        # escapes" raise — and a rescue commit is the factory's, not a
        # person's.
        sh(["git", "-c", "user.name=holophyte",
            "-c", "user.email=holophyte@factory.invalid", "commit", "-m",
            f"WIP: uncommitted leftovers preserved on reuse of {branch}"],
           cwd=wt)
        print(f"[holo2] preserved uncommitted leftovers as a WIP commit"
              f" on {branch}")
    if not is_ancestor("main", "HEAD"):
        # Preserved commits under a main that moved on: the review routes
        # and the merge gate both require main to be an ancestor of the
        # candidate, so left diverged the branch would raise out of every
        # review dispatch and stall the ticket on each rerun. Bringing main
        # in preserves the commits and restores the invariant; a conflict is
        # a human's merge to resolve, refused with the tree put back.
        r = subprocess.run(["git", "-c", "user.name=holophyte",
                            "-c", "user.email=holophyte@factory.invalid",
                            "merge", "--no-edit", "main"],
                           cwd=wt, capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=wt,
                           capture_output=True)
            return False, (f"preserved commits on {branch} conflict with a"
                           " main that moved on; a human resolves the merge"
                           " before this ticket is run again")
        print(f"[holo2] merged the moved-on main into preserved branch"
              f" {branch}")
    return True, ""


def run_task(task, conn=None, run_id=None, provider=None):
    """task: dict from a provider — {id, title, verify, budget_min}.

    Each task works in its own git worktree (TARGET stays on main, untouched),
    so a dirty/failed task can never block the repo or the next ticket.

    `conn` and `run_id` are the store and the claimed run the loop took the
    lease with. Every stage boundary below records its phase against them
    through `set_phase()`, and every review or adjudication turn records its
    round through `record_round()`, so in-flight state outlives the process:
    the run row, its rounds and its event stream say what the loop was doing
    and what the reviewer found, instead of that living only in this frame and
    in prose. Both default to None for a direct call with no store, which runs
    the same stages and records nothing.

    `provider` is the board the ticket came from, and the run needs it for one
    question only: at the merge gate, has the ticket's contract been edited
    since the claim froze it? A None provider — a direct call, a stub with no
    re-read — simply skips that check, and the gate is what it was.
    """
    task_id = task["id"]
    # The id the ticket is mirrored and re-read under, taken before `task` is
    # rebound to the title below.
    issue_id = mirror_key(task)
    # Claim-to-merge wall clock: run_task is entered immediately after the
    # claim, so this is the ticket's actual duration as far as the loop knows.
    started = monotonic()
    verify_cmd, budget_min = task.get("verify"), task["budget_min"]
    contracts = task.get("contracts")
    # The approved ticket body, kept before `task` collapses to its title: it
    # is the contract the implementer is held to at review, so the implementer
    # turn has to be given it verbatim rather than the one-line title the
    # branch is named after.
    body = (task.get("body") or "").strip()
    task = task["title"]
    # The name carries the ticket identifier ahead of the title slug: two
    # tickets whose titles agree for 30 characters must not share a branch or
    # a worktree, and a preserved `task/*` branch has to be traceable to its
    # ticket from `git branch` alone. The title portion keeps its own cap; the
    # identifier is added on top of it rather than eating into it.
    ident = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:30].strip("-")
    slug = f"{ident}-{slug}"
    branch = f"task/{slug}"
    wt = WORKTREES / slug
    # §4's one edge out of `claimed`, taken before the first git command:
    # cutting the worktree is already this run doing the ticket's work, so a
    # crash in it belongs to `working` and not to a run that still looks
    # freshly claimed.
    set_phase(conn, run_id, "working", f"cutting {branch} and implementing")
    if wt.exists():
        # leftover from a previous failed run — reuse it so preserved work
        # survives; the branch check below still gates on commits.
        ok, why = reuse_leftover(wt, branch)
        if not ok:
            ledger(task_id, f"FAILED to reuse leftover worktree for: {task}\n"
                            f"{why}\nNothing was deleted.")
            raise RunFailure(f"cannot reuse leftover worktree: {why}")
        # Whether the leftover actually holds anything, decided from content
        # rather than from which arm ran: an empty reuse was reset to main by
        # reuse_leftover() and is indistinguishable from a fresh cut, so the
        # close-outs below must neither claim preservation over nothing nor
        # keep an empty leftover alive forever.
        fresh = (not sh(["git", "status", "--porcelain"], cwd=wt)
                 and sh(["git", "rev-parse", "HEAD"], cwd=wt)
                 == sh(["git", "rev-parse", "main"], TARGET))
    else:
        # The mirror leftover: the branch exists but its directory does not
        # (a FAIL close-out preserves both; a human may clear only the
        # directory). `checkout -b` would die on it, and deleting the branch
        # could destroy preserved commits — so the run fails cleanly, the
        # same answer as the unregistered directory.
        if sh(["git", "branch", "--list", branch], TARGET):
            why = (f"branch {branch} already exists with no worktree; a"
                   " human moves it aside or deletes it before this ticket"
                   " is run again")
            ledger(task_id, f"FAILED to cut a fresh worktree for: {task}\n"
                            f"{why}\nNothing was deleted.")
            raise RunFailure(f"cannot cut a fresh worktree: {why}")
        sh(["git", "worktree", "add", "--detach", str(wt), "main"], TARGET)
        sh(["git", "checkout", "-b", branch], cwd=wt)
        fresh = True

    # The worktree exists and nothing has been dispatched into it yet, which
    # is the only moment the target's own setup can run: an implementer whose
    # toolchain is missing burns its whole budget discovering that, and a
    # worktree that silently borrows the main checkout's environment tests
    # something other than the branch it is on. A failure here is the run's
    # failure. A branch this run cut fresh is discarded -- no agent ran, so
    # there is no work on it to keep -- while a reused worktree may hold
    # preserved work the setup failure says nothing about, and is left
    # exactly as found. Either way no agent ran, so the failure is the
    # factory's plumbing, not evidence about the ticket: it closes out as
    # `InfraFailure` and does not spend one of the ticket's strikes.
    ok, out = run_worktree_setup(wt, conn, run_id)
    if not ok:
        print(out)
        # Ledger first: a deletion that itself fails must not also cost the
        # durable record of why the run stopped.
        if fresh:
            ledger(task_id, f"FAILED worktree setup for: {task}\nNo agent ran;"
                            f" branch {branch} holds nothing and is"
                            f" discarded.\n\n{out}")
            sh(["git", "worktree", "remove", "--force", str(wt)], TARGET)
            sh(["git", "branch", "-D", branch], TARGET)
            raise InfraFailure("worktree setup failed; no agent ran and the"
                               " empty branch was discarded")
        ledger(task_id, f"FAILED worktree setup for: {task}\nNo agent ran; "
                        f"reused worktree {wt} left in place with its "
                        f"work.\n\n{out}")
        raise InfraFailure(f"worktree setup failed; no agent ran; reused"
                           f" worktree and branch {branch} left in place with"
                           " their work")

    # The review base is main, not the HEAD reuse entered on: preserved
    # commits were never approved, so the reviewer must see them inside the
    # diff. Identical on a fresh cut, where HEAD is main.
    base_sha = sh(["git", "rev-parse", "main"], TARGET)
    # Where this run started, WIP commit and preserved commits included. The
    # no-commit gate below compares against this rather than main, so carried
    # leftovers cannot stand in for the implementer's own progress.
    start_sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    def timed(goal):
        """Run one agent turn with a hard wall-clock cap; None on timeout."""
        import signal

        def handler(signum, frame):
            raise TimeoutError()

        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(budget_min * 60)
        try:
            return agent("implement", goal, wt)
        except TimeoutError:
            print(f"[holo2] task exceeded {budget_min} min budget")
            return None
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    # 1. implement — the ticket verbatim: title, then the approved body, then
    # the verify commands the gate will actually run. The same `ticket` text is
    # what the reviewer and the adjudicator below judge against, so all three
    # turns are held to one contract. A ticket with no body
    # (a file-backed task line, a stub provider) degrades to the title alone.
    ticket = f"{task}\n\n{body}" if body else task
    commands = (f"\n\nThese verify commands must pass before review and again "
                f"before merge:\n\n{verify_cmd}" if verify_cmd else "")
    out = timed(f"Implement this task in this repo:\n\n{ticket}{commands}\n\n"
                "The ticket above is the contract, acceptance criteria "
                "included; the task is done only when they hold. Commit your "
                "work with a clear message. Stay strictly on-scope; do not "
                "expand the task.")
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    # A reused branch whose tip already differs from main carries a candidate
    # an earlier run left behind. An implementer handed finished work
    # correctly adds nothing to it, so reading that as "no progress" failed
    # the ticket on every relaunch and left operator surgery or destroying the
    # work as the only exits (holophyte-bugs #3). The carried tip is the
    # candidate instead, and it still owes verify and review below.
    carried = not fresh and bool(
        subprocess.run(["git", "diff", "--quiet", "main", "HEAD"],
                       cwd=wt, capture_output=True).returncode)
    if head == start_sha and not carried:
        print(f"[holo2] implementer made no commits for: {task}")
        if fresh:
            sh(["git", "worktree", "remove", "--force", str(wt)], TARGET)
            sh(["git", "branch", "-D", branch], TARGET)
            raise RunFailure("implementer made no commits; the empty branch"
                             " and worktree were discarded")
        # A reused worktree holds work some earlier run preserved; this
        # run's implementer adding nothing is no reason to destroy it.
        raise RunFailure(f"implementer made no new commits; preserved work"
                         f" kept on {branch} at {start_sha[:12]}")
    if head == start_sha:
        note = (f"candidate carried from a prior run; implementer added"
                f" nothing to {branch} at {start_sha[:12]}")
        print(f"[holo2] {note}")
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "carried_candidate", note)
    if out is None:
        # The budget alarm fired *after* real commits landed. A timeout is
        # not "no work": destroying the commits here would repeat the
        # incident this path exists to prevent.
        raise RunFailure(f"implementer exceeded the {budget_min} min budget;"
                         f" work kept on {branch} at {head[:12]}")

    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    def verify_brief(ok, out):
        """The verify result as the reviewer sees it — omitted when the ticket
        declares no command, so the brief never implies a gate that never ran."""
        if not verify_cmd:
            return ""
        return (f"A mechanical verification command was run and "
                f"{'PASSED' if ok else 'FAILED with output below'}:\n{out}\n")

    # 2. review rounds (MAX_ROUNDS). Verify runs before each review and its
    # result goes into the brief; every round that is not a clean approval —
    # round 2 included — gets a fix round, because a round-2 blocker is the
    # cheapest fix in the loop and used to need a human to close it out.
    for rnd in range(1, MAX_ROUNDS + 1):
        set_phase(conn, run_id, "verifying", f"round {rnd}: verify before review")
        ok, out = run_verify(verify_cmd, wt, contracts)
        if ok:
            print(f"[holo2] verify ok before round {rnd}")
        else:
            print(f"[holo2] verify FAILED before round {rnd}:\n{out}")

        set_phase(conn, run_id, "reviewing", f"round {rnd} review")
        round_started = int(time() * 1000)
        verdict = agent("review",
            f"You are a READ-ONLY code reviewer. Review commit {sha} using "
            "refs/review/base as the frozen base and refs/review/candidate as "
            "the candidate "
            "in this repo against the ticket below. The ticket is the "
            "contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + verify_brief(ok, out)
            + "Do not modify anything. End your reply with exactly one line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha=base_sha, candidate_sha=sha)
        # Before the approval check, so the round that ends the loop is stored
        # like every other one: a review the store has no row for is a round
        # §6 cannot compare the next one against.
        record_round(conn, run_id, rnd, "review", verdict, verify_cmd, ok, out,
                     started_at=round_started)

        if ok and review_runner.terminal_verdict(verdict) == "APPROVE":
            break

        # 3. implementer addresses findings (same branch, new commit)
        set_phase(conn, run_id, "addressing", f"round {rnd}: addressing findings")
        fixes = timed("A reviewer left findings on your work. The ticket you "
                      "are held to, acceptance criteria included:\n\n"
                      f"{ticket}\n\nReviewer findings:\n\n{verdict}\n\n"
                      "For EACH finding, adjudicate it first: ADDRESS (concrete "
                      "blocker — fix now), FOLLOW_UP (valid but out of scope — name "
                      "it in the commit message), or DECLINE (invalid/out-of-scope — "
                      "state the rationale in the commit message). Then fix only the "
                      "ADDRESS items and commit.")
        ledger(task_id, f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
                     f"Reviewer findings:\n{verdict}\n\n"
                     f"Implementer response:\n{fixes}")
        if fixes is None or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
            print(f"[holo2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            raise RunFailure(f"fix round {rnd} timed out or made no progress;"
                             f" branch {branch} preserved at {sha[:12]}")
        sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    else:
        # 3b. Terminal adjudication: both review rounds and their fixes are
        # spent, so one fresh independent run issues a bare verdict on the
        # final state. There is no further fix round under any outcome —
        # anything but PASS preserves the branch and stops the loop.
        set_phase(conn, run_id, "verifying", "verify before terminal adjudication")
        ok, out = run_verify(verify_cmd, wt, contracts)
        if not ok:
            print(f"[holo2] verify FAILED before adjudication; leaving branch "
                  f"{branch} (worktree {wt}) at {sha} for a human:\n{out}")
            ledger(task_id, f"FAILED verify before terminal adjudication after "
                         f"{MAX_ROUNDS} review rounds; branch {branch} preserved "
                         f"at {sha}\n\n{out}")
            raise RunFailure(f"verify failed before terminal adjudication;"
                             f" branch {branch} preserved at {sha[:12]}")
        print("[holo2] verify ok before adjudication")

        set_phase(conn, run_id, "reviewing", "terminal adjudication")
        round_started = int(time() * 1000)
        reply = agent("adjudicate",
            f"You are a READ-ONLY final adjudicator. Judge commit {sha} using "
            "refs/review/base as the frozen base and refs/review/candidate as "
            "the candidate "
            "in this repo against the ticket below. The ticket is the "
            "contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + verify_brief(ok, out)
            + "This candidate has already had its review rounds and their "
            "fixes; no further fix round exists. Your job is a verdict on the "
            "state as it stands, not a review.\n"
            "Do not modify anything. Do NOT list findings, request changes, or "
            "propose follow-up work — a reply that reads as a findings list is "
            "not a verdict and is treated as FAIL. Give at most one short "
            "paragraph of justification, then exactly one final line:\n"
            "VERDICT: PASS  or  VERDICT: FAIL\n"
            "PASS means the candidate is mergeable as it stands.", wt,
            base_sha=base_sha, candidate_sha=sha)
        # The adjudication is a round of the run like the reviews before it —
        # numbered after them, so the run's rounds read in the order they
        # happened.
        record_round(conn, run_id, MAX_ROUNDS + 1, "adjudicate", reply,
                     verify_cmd, ok, out, started_at=round_started)
        try:
            decision = review_runner.terminal_verdict(
                reply, review_runner.ADJUDICATION_VERDICTS)
        except review_runner.ReviewBoundaryError:
            decision = "MALFORMED"  # no clean verdict — read as FAIL
        if decision != "PASS":
            print(f"[holo2] terminal adjudication: {decision}; leaving branch "
                  f"{branch} (worktree {wt}) at {sha} for a human. Task: {task}")
            ledger(task_id, f"Terminal adjudication after {MAX_ROUNDS} review "
                         f"rounds: {decision}; branch {branch} preserved at "
                         f"{sha}\n\nAdjudicator reply:\n{reply}")
            raise RunFailure(f"terminal adjudication: {decision};"
                             f" branch {branch} preserved at {sha[:12]}")
        print("[holo2] terminal adjudication: PASS")
        ledger(task_id, f"Terminal adjudication after {MAX_ROUNDS} review "
                     f"rounds: PASS\n\nAdjudicator reply:\n{reply}")

    # 4. pre-merge verify (catches fix-round regressions), then merge. Both
    # happen under `merge_gate`: §4's gate node is the one edge out of a
    # passing review, and this verify is the mechanical half of what the gate
    # asks. Under the `personal` autonomy profile the human half is a no-op,
    # so the run passes through the node rather than around it and a failed
    # pre-merge verify is a run stopped at the gate.
    set_phase(conn, run_id, "merge_gate", "pre-merge verify, then the autonomy gate")
    ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
        print(f"[holo2] verify FAILED before merge; leaving branch {branch} "
              f"at {sha} for a human:\n{out}")
        ledger(task_id, f"FAILED verify before merge; branch {branch} "
                        f"preserved at {sha}\n\n{out}")
        raise RunFailure(f"verify failed before merge; branch {branch}"
                         f" preserved at {sha[:12]}")
    print("[holo2] verify ok before merge")

    # The other half of the gate, and the one a mechanical verify cannot ask:
    # this candidate was implemented, reviewed and verified against the ticket
    # as it stood at the claim, so a body edited since then means the work
    # answers a contract that no longer exists. Merging it would land code
    # nobody approved against the ticket as it now reads, and the honest
    # answer is the one every other refusal at this gate gives — leave the
    # branch and its worktree for a human.
    drift = merge_drift(conn, run_id, provider, issue_id)
    if drift:
        warn_on_run(conn, run_id,
                    f"{task_id} changed while the run was working "
                    f"({', '.join(drift)}); not merging {branch} at {sha} — "
                    "the candidate answers the ticket as it was claimed, not "
                    "as it now reads")
        ledger(task_id, "MERGE REFUSED: the ticket drifted from the contract "
                        f"this run was claimed under ({', '.join(drift)}). "
                        f"Branch {branch} preserved at {sha}. Work it again "
                        "against the body as it now reads, or restore the "
                        "body the run was claimed under.")
        raise RunFailure(f"ticket drifted from the claimed contract"
                         f" ({', '.join(drift)}); branch {branch} preserved"
                         f" at {sha[:12]}")

    # Commit any pending FINDINGS.md changes BEFORE merging so the merge
    # never trips over a dirty index. Nothing is written to the file during a
    # run any more, so this is normally a no-op; what it still catches is a
    # window an earlier failed run regenerated and left uncommitted.
    commit_findings(f"FINDINGS: {task_id} review records")

    # `squashing` is skipped, not faked: this merge is --no-ff and rewrites
    # no history, so the run goes merging -> done and the phase §4 puts
    # between them names an activity that never happens here.
    set_phase(conn, run_id, "merging", f"--no-ff merge of {branch} into main")
    mr = subprocess.run(["git", "merge", "--no-ff", branch, "-m",
                         f"Merge {branch}: {task}"], cwd=TARGET,
                        capture_output=True, text=True)
    if mr.returncode != 0:
        # What conflicted is the index's answer, not the merge output's: a
        # substring search over stdout+stderr also matches a conflict in
        # `docs/FINDINGS.md-notes.md`, or one whose message merely mentions
        # the file, and would then "resolve" a conflict nobody looked at.
        conflicted = sorted(
            p for p in subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"], cwd=TARGET,
                capture_output=True, text=True).stdout.splitlines() if p.strip())
        if conflicted == ["FINDINGS.md"]:
            # conflict limited to FINDINGS.md — prefer the branch side (fuller log)
            subprocess.run(["git", "checkout", "--theirs", "FINDINGS.md"],
                           cwd=TARGET, capture_output=True, text=True)
            sh(["git", "add", "FINDINGS.md"], TARGET)
            sh(["git", "commit", "--no-edit"], TARGET)
        else:
            # Anything else is a human's merge to make. An `assert` here was
            # both stripped under `python -O` and, when it did fire, left main
            # sitting on a half-applied merge with an unresolved index while
            # the run died mid-frame. Abort first, so main is the integration
            # point it was before the attempt, and fail the run through the
            # same close-out every other refusal at this gate uses — branch
            # and worktree preserved.
            subprocess.run(["git", "merge", "--abort"], cwd=TARGET,
                           capture_output=True, text=True)
            dirty = subprocess.run(["git", "status", "--porcelain"], cwd=TARGET,
                                   capture_output=True, text=True).stdout.strip()
            paths = ", ".join(conflicted) or "(no unmerged paths reported)"
            why = (f"merge of {branch} into main conflicted on: {paths};"
                   f" branch and worktree preserved")
            if dirty:
                # The abort did not restore main: say so in the reason rather
                # than let the next run discover it.
                why += (" — main is NOT clean after the abort: "
                        + " ".join(dirty.split()))
            print(f"[holo2] {why}")
            ledger(task_id, f"MERGE ABORTED: conflict on {paths}. Branch "
                            f"{branch} preserved at {sha}. Rebase it on main "
                            "and re-run, or merge it by hand.")
            raise RunFailure(why)
    # The merge has landed: the branch holds nothing main does not, so the
    # worktree's stray untracked files are not preserved work — and a cleanup
    # refusal must not re-classify merged work as a failed run.
    try:
        sh(["git", "worktree", "remove", "--force", str(wt)], TARGET)
        sh(["git", "branch", "-d", branch], TARGET)
    except RuntimeError as e:
        print(f"[holo2] post-merge cleanup left debris: {e}")
    # Nothing tells Linear the ticket is done here any more. The merge makes
    # the ticket `merged` in the store, and `main()` projects that status onto
    # the board through `mirror_push()` once the run has been released — one
    # writer of the workflow state instead of a call from the middle of a run
    # that has not finished ending yet.
    # One greppable line of timing data per merged ticket: the estimate stays
    # write-only otherwise, and a future burndown script reads this format.
    actual_min = (monotonic() - started) / 60
    ledger(task_id, f"MERGED to main (branch {branch} deleted). "
                 f"Verify: {'passed' if ok else 'n/a'}.\n"
                 f"actual: {actual_min:.1f} min · estimate: {budget_min} min · "
                 f"rounds: {rnd}")
    # The task's own commit of FINDINGS.md is `main()`'s, not this frame's:
    # the run's close-out entry exists only once the run has been released,
    # which happens after this returns.
    print(f"[holo2] merged: {task}")
    return True


# --- store seam --------------------------------------------------------------
# Every store call the loop makes goes through one of the helpers below, so a
# later wiring ticket extends this seam instead of threading SQL through
# run_task().


def open_store(path=None):
    """Open the loop's store, creating and migrating the schema if needed.

    The store's directory is made here, on first need: `retarget()` only
    derives paths, and a `--report` against a target that has no store says
    so without leaving an empty directory behind.
    """
    path = Path(path or STORE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = store.open(str(path))
    store.init(conn)
    return conn


def set_phase(conn, run_id, phase, note=None):
    """Record a stage boundary: the run's phase, its heartbeat, one event.

    The loop's single writer of `runs.phase` (state-model §6), which is why
    every stage below calls it instead of writing its own row: phase,
    heartbeat and narrative event move together in the store or not at all, so
    a crashed loop leaves a run parked where it stopped rather than parked in
    whatever phase it was last seen entering.

    A `conn` of None makes this a no-op, for `run_task()` driven directly
    without a store. That keeps one set of call sites rather than a storeless
    copy of the loop, and the phases are then simply not recorded.
    """
    if conn is None:
        return
    store.set_phase(conn, run_id, phase, note)


def record_round(conn, run_id, rnd, role, reply, verify_cmd, ok, out,
                 started_at=None):
    """Record one review or adjudication round as a `reviewRounds` row.

    The round the loop just ran, as the store holds it: the verdict, the
    reviewer route that issued it, the verify result the reviewer was briefed
    with, and the findings — structured where the reply let them be extracted.
    `store.record_review_round()` fingerprints them on the way in, which is
    what makes two rounds comparable and is the whole reason the prose in
    FINDINGS.md is not enough.

    Which findings a round carries follows the round's kind. A `review` round
    that asked for changes carries what parsed out of its reply; an approval
    carries none, because approving prose is not a findings list. An
    adjudication is a verdict and nothing else by its own prompt, so PASS and
    FAIL both store an empty list — a reply that named no verdict at all is
    the exception, and its raw text is kept as the one finding rather than
    recorded as a round that said nothing.

    A `conn` of None makes this a no-op, like `set_phase()`, so a storeless
    `run_task()` runs the same stages and records nothing.
    """
    if conn is None:
        return
    verdicts = (review_runner.REVIEW_VERDICTS if role == "review"
                else review_runner.ADJUDICATION_VERDICTS)
    verdict = round_verdict(reply, verdicts)
    if verdict == "error":
        findings = [raw_finding(reply)]
    elif verdict == "changes_requested" and role == "review":
        findings = parse_findings(reply)
    else:
        findings = []
    # `run_verify()` reports a pass/fail gate rather than a raw status — the
    # failing clause and its exit code live in the output it builds — so the
    # exit code stored here is that verdict, and `output` is the detail.
    results = ([{"command": verify_cmd, "exitCode": 0 if ok else 1,
                 "output": out}] if verify_cmd else [])
    store.record_review_round(conn, run_id, rnd, verdict, agent_route(role),
                              findings=findings, verification_results=results,
                              started_at=started_at,
                              ended_at=int(time() * 1000))


def warn_on_run(conn, run_id, summary):
    """Print a warning and record it against `run_id`; never raise.

    The half of `warn()` that a caller already holding a run id uses directly.
    Best-effort work that failed is still part of the run's account of itself,
    so it lands in the same event stream as the phase changes rather than only
    on stdout. A missing store or run leaves the printed line as the whole
    record, which is the same no-op `set_phase()` makes for a storeless
    `run_task()`.
    """
    print(f"[holo2] {summary}")
    if conn is None or run_id is None:
        return
    store.record_event(conn, run_id, "warning", summary)


# --- FINDINGS.md as a window over the rows ------------------------------------
# The ledger used to be the source of truth, appended to once per agent turn,
# and it reached 53 KB in a single day: unreadable for a human and a context
# hazard for an agent working in the checkout. The rows are the history now --
# complete, queryable, cheap -- and the file is a rendering of their recent
# tail, so a reader gets the last few runs and everything older is one query
# away rather than deleted.
#
# The rendering is a function of the rows and of nothing else: no clock, no
# environment, ties in the ordering broken by row id, and the sanitizing
# already done at the row write. That is what makes regenerating the whole file
# at every close-out reviewable -- identical rows produce identical bytes, so
# the diff of a regeneration is exactly the entries the run added.
FINDINGS_WINDOW = 25  # entries kept in the file; per-project config is future work
# What separates the frozen pre-store history from the rendered window. Every
# regeneration reproduces the bytes above this line and replaces everything
# below it, so a ledger written before the store existed is preserved by not
# being rendered at all.
FINDINGS_MARKER = "<!-- store-rendered below -->"
FINDINGS_ARCHIVE = "[{n} earlier entries in holophyte.db — query runs/reviewRounds]"
# One finding is one line of the window: enough to recognize the complaint by,
# with the row holding all of it.
FINDING_LINE_CHARS = 160


STAMP_UNREADABLE = "(unreadable timestamp)"


def _ms(value):
    """`value` as epoch milliseconds, or None when the column does not hold one.

    The timestamp columns are declared INTEGER but SQLite affinity does not
    enforce it, so a row can carry text, NULL, or a float no calendar reaches.
    Anything that is not a finite number the renderer treats as unreadable.
    """
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _stamp(ms):
    """Epoch milliseconds as the ledger's UTC timestamp.

    A stamp the row cannot supply renders as a visible placeholder rather than
    raising: one bad row must not take the whole window with it.
    """
    from datetime import datetime, timezone
    ms = _ms(ms)
    if ms is None:
        return STAMP_UNREADABLE
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return STAMP_UNREADABLE


def _entry(at, ticket, lines):
    """One entry: the heading the file has always used, then its body."""
    return "\n".join([f"## {_stamp(at)} — {ticket}", *lines])


def _gist(text):
    """`text` collapsed to one line and cut to the window's width."""
    gist = " ".join(str(text).split())
    if len(gist) > FINDING_LINE_CHARS:
        gist = gist[:FINDING_LINE_CHARS].rstrip() + "…"
    return gist


def _document(text):
    """A stored JSON document column decoded to its list, or None.

    None for anything the schema's `[]` comment does not describe: text that
    is not JSON, JSON nested past what the decoder's stack can walk, or JSON
    that is not an array. The writer refuses all three, so a row like this was
    written past it -- by hand, by an earlier release, or by corruption -- and
    the renderer's job is to show that, not to crash the close-out that
    regenerates every other entry in the window.

    The caught set is wider than `json.JSONDecodeError` on purpose. A column
    SQLite hands back as a BLOB decodes through `UnicodeDecodeError`, which is
    a `ValueError` but not a `JSONDecodeError`, so catching the base class is
    what makes "never raises" hold for a bytes column rather than only for bad
    syntax; `TypeError` covers a column that is not text at all, and
    `RecursionError` — not a `ValueError` — the pathologically nested document.
    """
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, list) else None


def finding_line(finding):
    """One stored finding as one line: where, how bad, and the gist.

    The message is collapsed to a single line and cut, because the window is a
    place to notice a complaint rather than to read it: the whole message is in
    `reviewRounds.findings`, and the alternative -- full prose per finding --
    is the unbounded file this rendering replaces.

    Never raises. A finding that is not a mapping with a `path` and a
    `severity` is rendered as a marked line carrying its compact repr, so the
    row's shape is visible in the window rather than fatal to it.
    """
    if (not isinstance(finding, dict) or "path" not in finding
            or "severity" not in finding):
        return f"- (malformed finding) {_gist(repr(finding))}"
    where = str(finding["path"])
    if finding.get("line"):
        where = f"{where}:{finding['line']}"
    # The reviewer's own bullet marker opens most stored messages; the line
    # this renders already is a bullet, so it is dropped rather than nested.
    gist = BLOCK_BREAK_RE.sub("", _gist(finding.get("message", "")), count=1)
    return f"- {where} [{finding['severity']}] {gist}".rstrip()


def round_entry(row):
    """One `reviewRounds` row as an entry: the verdict and what it filed.

    Never raises on the row's two JSON columns. A `verificationResults` or
    `findings` document that does not decode to the schema's array, or an
    array holding a result that is not a mapping, renders as an `unreadable`
    verify note or an `unparseable` findings line quoting the raw column,
    rather than as a verdict the row does not actually carry: the writer
    refuses such rows, so one that exists is evidence to show, and a render
    that died on it would leave the file stale for every good row after it.
    """
    at, ticket, number, verdict, model, results, findings = row
    results = _document(results)
    verify = ""
    if results is None or not all(isinstance(r, dict) for r in results):
        verify = " · verify unreadable"
    elif results:
        verify = (" · verify "
                  + ("passed" if all(r.get("exitCode") == 0 for r in results)
                     else "failed"))
    raw_findings, findings = findings, _document(findings)
    lines = [f"Round {number}: {verdict} · reviewer {model}{verify}"]
    if findings is None:
        lines.append(f"Findings: unparseable — {_gist(raw_findings)}")
    elif findings:
        lines.append(f"Findings ({len(findings)}):")
        lines.extend(finding_line(finding) for finding in findings)
    return _entry(at, ticket, lines)


def run_entry(row):
    """One ended `runs` row as an entry: its outcome and the timing line.

    Every number on the timing line is read off the run row itself -- the two
    stamps, the estimate it was claimed under, and the round count stamped at
    close-out (terminal adjudication included) -- rather than off the loop's
    in-frame counters or the ticket's current estimate. That is what makes the
    line and the row say the same thing: `--report` queries the same columns,
    and a rendering that needs the loop's variables is not a rendering of the
    rows.
    """
    at, ticket, outcome, reason, branch, started, time_box, rounds = row
    if outcome == "merged":
        head = ("MERGED to main"
                + (f" (branch {branch} deleted)" if branch else "") + ".")
    else:
        head = (outcome or "ended").upper()
        head += f": {' '.join(reason.split())}" if reason else "."
    # Byte-stable: a burndown script greps this line.
    estimate = f"{time_box // 60000} min" if _ms(time_box) else "n/a"
    ended, started = _ms(at), _ms(started)
    actual = ("n/a" if ended is None or started is None
              else f"{(ended - started) / 60000:.1f} min")
    return _entry(at, ticket, [
        head,
        f"actual: {actual} · estimate: {estimate} · rounds: {rounds}",
    ])


def findings_entries(conn):
    """Every entry the store holds, oldest first.

    Two row kinds are entries: a review round, and a run that ended. A run
    still in flight has no entry -- it has no outcome yet, and an entry that
    changed on the next render would make the file churn.
    """
    rounds = conn.execute(
        "SELECT COALESCE(rr.endedAt, rr.startedAt), t.linearIdentifier,"
        " rr.round, rr.verdict, rr.reviewerModel, rr.verificationResults,"
        " rr.findings, rr.id"
        " FROM reviewRounds rr JOIN runs r ON r.id = rr.runId"
        " JOIN tickets t ON t.id = r.ticketId").fetchall()
    runs = conn.execute(
        "SELECT r.endedAt, t.linearIdentifier, r.outcome, r.outcomeReason,"
        " r.branch, r.startedAt, r.timeBoxMs, r.reviewRoundCount, r.id"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NOT NULL").fetchall()
    entries = [(row[0], "round", row[-1], round_entry(row[:-1])) for row in rounds]
    entries += [(row[0], "run", row[-1], run_entry(row[:-1])) for row in runs]
    # Two rows stamped the same millisecond still have one order: kind, then
    # the id the database gave them. A row with no readable stamp cannot be
    # placed in time, so it sorts ahead of every dated one -- deterministic,
    # and never a comparison between a number and whatever the column held.
    def _order(entry):
        at = _ms(entry[0])
        return (at is not None, at or 0, entry[1], entry[2])
    entries.sort(key=_order)
    return [entry[3] for entry in entries]


def render_findings(conn, preamble=""):
    """The whole of FINDINGS.md: `preamble`, the marker, then the window.

    Only the newest `FINDINGS_WINDOW` entries are rendered; the rest collapse
    into the one archive line that says how many there are and where to read
    them. Newest last, so the file reads forwards like the append-only ledger
    it replaces.
    """
    entries = findings_entries(conn)
    hidden = max(0, len(entries) - FINDINGS_WINDOW)
    blocks = [FINDINGS_ARCHIVE.format(n=hidden)] if hidden else []
    blocks += entries[hidden:]
    # The preamble is reproduced byte for byte and only padded away from the
    # marker, which keeps this idempotent: the padded text is what the next
    # render reads back as the preamble.
    if preamble and not preamble.endswith("\n\n"):
        preamble += "\n" if preamble.endswith("\n") else "\n\n"
    body = "\n\n".join(blocks)
    return preamble + FINDINGS_MARKER + "\n" + (f"\n{body}\n" if body else "")


def frozen_preamble(text):
    """The half of `text` a regeneration must reproduce: above the marker.

    A file with no marker is entirely pre-store history, so all of it is
    preamble and the window is written below it. The marker counts only as a
    line of its own, which is what keeps a reviewer who quoted it from moving
    the boundary: no line this module renders can be a bare marker -- every
    one of them is a heading, a `Round`/outcome line, a `- ` finding or the
    archive line -- so the first standalone marker line is always the real one.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == FINDINGS_MARKER:
            return "".join(lines[:i])
    return text


def write_findings(conn, path=None):
    """Regenerate FINDINGS.md in place, keeping everything above the marker."""
    path = Path(path) if path else TARGET / "FINDINGS.md"
    existing = path.read_text() if path.exists() else ""
    path.write_text(render_findings(conn, frozen_preamble(existing)))
    return path


def commit_findings(message):
    """Commit FINDINGS.md in the target checkout, if the render changed it.

    Returns whether it committed. The guard is not an optimization: a
    regeneration that produced the same bytes has nothing to record, and
    `git commit` on an unchanged tree fails.
    """
    r = subprocess.run(["git", "status", "--porcelain", "FINDINGS.md"],
                       cwd=TARGET, capture_output=True, text=True)
    if not r.stdout.strip():
        return False
    sh(["git", "add", "FINDINGS.md"], TARGET)
    sh(["git", "commit", "-m", message], TARGET)
    return True


def refresh_findings(conn):
    """Render the window over the store's rows into the target's FINDINGS.md.

    A `conn` of None makes this a no-op, like `set_phase()` and
    `record_round()`: a storeless `run_task()` has no rows to render, and the
    file it would otherwise overwrite with an empty window is left alone.
    """
    if conn is None:
        return
    write_findings(conn)


def mirror_key(task):
    """The `linearIssueId` a task's mirror is keyed by.

    The canonical issue UUID when the provider has one, and the human label
    otherwise. Written once here because two callers now have to agree on it:
    `mirror_task()` mirrors under this id, and the failure-pattern check below
    has to find that same row *before* anything is claimed.
    """
    return task.get("issue_id") or task["id"]


def store_status(conn, ticket_id):
    """The store's status column for `ticket_id`, for a printed decision."""
    return conn.execute("SELECT status FROM tickets WHERE id = ?",
                        (ticket_id,)).fetchone()[0]


def task_contract(task):
    """A provider task's contract as `(title, criteria, commands)`.

    The one mapping from the provider's shape to the store's, used by both
    sides of the drift check: `mirror_task()` mirrors the ticket through it, so
    the snapshot `store.claim()` freezes off that row is this contract, and
    `merge_drift()` snapshots the live ticket the same way. Two hand-rolled
    mappings would eventually disagree about, say, a ticket carrying no verify
    command, and the disagreement would read as drift on a ticket nobody
    touched. Positional, in the order `store.contract_snapshot()` takes them.
    """
    return (task["title"],
            list(task.get("criteria") or ()),
            [task["verify"]] if task.get("verify") else [])


def merge_drift(conn, run_id, provider, issue_id):
    """The contract fields that moved between the claim and now; () if none.

    The merge gate's question: the run was implemented, reviewed and verified
    against the ticket as it stood at the claim, so a body a human edited
    while the run was working means the candidate answers a contract that no
    longer exists. Asked here rather than continuously because this is the
    last moment the answer can still change anything — before it, an edit is
    something a fix round could absorb; after it, the branch is in main.

    Best-effort in one direction only: a provider that cannot re-read a
    ticket, a Linear that is down, an issue that has been deleted, and a run
    claimed before the snapshot column existed all return `()`. That is not
    the same claim as "nothing changed" — it is "this gate has no evidence",
    and refusing a verified merge on missing evidence would turn every Linear
    outage into a stuck queue. A failed read is a warning on the run so the
    silence is at least recorded; drift itself is the caller's to act on.
    """
    if conn is None or run_id is None or provider is None:
        return ()
    fetch = getattr(provider, "fetch_task", None)
    if fetch is None:
        return ()  # a provider with no re-read; nothing to compare against
    claimed = store.run_contract(conn, run_id)
    if claimed is None:
        return ()  # claimed before the snapshot existed
    try:
        live = fetch(issue_id)
    except Exception as e:
        warn_on_run(conn, run_id, f"could not re-read {issue_id} for the "
                                  f"merge-time drift check ({e}); merging on "
                                  "the contract frozen at the claim")
        return ()
    if not live:
        warn_on_run(conn, run_id, f"{issue_id} could not be found for the "
                                  "merge-time drift check; merging on the "
                                  "contract frozen at the claim")
        return ()
    return store.contract_drift(
        claimed, store.contract_snapshot(*task_contract(live)))


def mirror_task(conn, project, task):
    """Mirror the offered ticket's live body into the store; return its id.

    The first half of a claim, split from the lease so the loop can ask the
    store about the ticket *as it is now* before it opens a run. The mirror is
    an upsert with no lease of its own, so a ticket that turns out to be
    unpickable has cost nothing but a refreshed row — which is the row the
    next offer is judged on. The lease itself is `store.claim()`, taken by
    `main()` once the fresh row has said yes; that is what stops two loops
    from working one project at once.

    Where the mirror lands is the store's routing rule (§2) applied to what
    the provider parsed: a ticket carrying both acceptance criteria and a
    verify command is `ready`, and one missing either is `needs_spec` and not
    pickable. The loop still picks in Linear, so what that decides here is
    whether the ticket can legally enter `in_flight` — an under-specced mirror
    cannot, and the board simply keeps saying whatever a human last set.

    The two ids the mirror stores are different ids: `linearIssueId` is the
    canonical issue UUID the provider hands over as `issue_id`, and is what
    the mirror is keyed and re-found by, while `linearIdentifier` is the human
    "KO-123" label. Storing the label in both would key the mirror on the
    mutable one, so a later UUID-carrying writer — webhook wiring, say — would
    mirror the same issue a second time under its real id. A provider with no
    UUID to give still gets a mirror, keyed on the identifier it does have.

    No `depends_on`, on purpose: the provider does not parse a dependency
    list, so the store's copy is the only one, and `store.mirror_ticket()`
    keeps it when the caller says nothing. Passing `[]` here instead would
    clear a blocked ticket's dependencies in the very row the pickability
    gate reads next.
    """
    title, criteria, commands = task_contract(task)
    return store.mirror_ticket(
        conn,
        project,
        linear_issue_id=mirror_key(task),
        linear_identifier=task["id"],
        title=title,
        acceptance_criteria=criteria,
        verification_commands=commands,
        time_box_ms=task["budget_min"] * 60 * 1000,
    )


def release_run(conn, run_id, merged, reason=None, outcome_class="work"):
    """Give the lease back when the loop is done with a run, merged or not.

    Called from the loop's `finally`, because the failure paths are the ones
    that matter: a run that dies holding the lease blocks every later claim on
    the project, and a preserved branch is meant to wait for a human without
    also freezing the queue.

    A failure reason names the phase the run stopped in, read back from the
    store rather than remembered here: on a crash the loop's own idea of where
    it was died with the exception, while the phase `set_phase()` last wrote
    is exactly where the run got to. A caller that knows better says so with
    `reason` — the supervisor sweep does, because "stopped in phase working"
    is true of a swept run and says nothing about why it was swept.
    """
    if merged:
        store.release(conn, run_id, "merged")
        return
    # No preservation claim in the default: the paths that delete or keep a
    # branch say so themselves in the reason they pass, and stamping
    # "preserved" on a reason-less failure lied on every deletion path
    # (KO-146 incident, run 10).
    store.release(conn, run_id, "failed", reason or
                  f"run stopped in phase {store.run_phase(conn, run_id)}",
                  outcome_class=outcome_class)


# --- Linear as the notice board ----------------------------------------------
# State-model §1: Holophyte owns the in-flight substate and Linear is where it
# gets posted, so ticket status travels one way — store to Linear, never read
# back — and `mirror_push()` is the loop's only writer of a workflow state.
# That is what stops loop logic from depending on which column a human dragged
# a card into: the drag is overwritten by the next push, and nothing branches
# on it.
#
# The table is module-level config rather than five literals at the call sites
# because the thing likeliest to change is the mapping, not the pushing —
# per-project mappings and §9's team-vs-project question (does a project or a
# team own these state names?) both land here. Neither is answered here.
MIRROR_STATES = {
    "ready": "Todo",
    "in_flight": "In Progress",
    "merged": "Done",
    "abandoned": "Canceled",
    # No Linear state means "waiting on an operator", so a blocked ticket goes
    # back to the column a human picks work out of, until there is a state
    # that says it properly. Telling the operator what is being waited on is a
    # comment, and status comments are not this projection's business.
    "blocked_on_operator": "Todo",
}
# `needs_spec` and `blocked_on_deps` are deliberately unmapped: neither is a
# statement about the board — an unspecced ticket is wherever its author left
# it, and a dependency-blocked one is the resolver's business — so the
# projection leaves those tickets' Linear state alone rather than inventing a
# column for them.


def warn(conn, ticket_id, summary):
    """Record a warning on the ticket's run and print it; never raise.

    Best-effort work that failed is still something the run has to account
    for, so it goes in the same event stream as the phase changes, as a
    `warning` row a reader can pick out of the narrative. It is written
    against the ticket's run — the active one, or the last one when the run
    has already been released — because that is the stream a reader looking
    at this ticket is already reading.
    """
    run_id = None
    if conn is not None:
        row = conn.execute(
            "SELECT COALESCE(activeRunId, lastRunId) FROM tickets WHERE id = ?",
            (ticket_id,)).fetchone()
        # A ticket with no run has nothing to hang the row on; the printed
        # line is then the whole record.
        run_id = row[0] if row else None
    warn_on_run(conn, run_id, summary)


def mirror_push(conn, ticket_id, provider=None):
    """Project the ticket's stored status onto its Linear state; return it.

    Last-write-wins and one-way: the store's `tickets.status` is the truth,
    `MIRROR_STATES` says what that truth looks like on the board, and nothing
    is read back. Returns the state pushed, or None when there was nothing to
    push — an unmapped status, or a push that failed.

    Failure is a warning, not an error. Linear being unreachable must not fail
    a run that has already merged, so a push that raises leaves the board
    stale, the store right, and a `warning` row in the run's stream saying
    which projection did not land. That is the failure §1 asks for: the notice
    board is allowed to be behind, the loop is not allowed to be wrong.

    A `conn` of None makes this a no-op, like the rest of the store seam: a
    storeless `run_task()` holds no status to project.
    """
    if conn is None:
        return None
    row = conn.execute(
        "SELECT linearIssueId, linearIdentifier, status FROM tickets"
        " WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise ValueError(f"no ticket {ticket_id}")
    issue_id, identifier, status = row
    state = MIRROR_STATES.get(status)
    if state is None:
        return None
    if provider is None:
        import linear_provider as provider
    try:
        provider.set_state(issue_id, state)
    except Exception as e:
        warn(conn, ticket_id, f"Linear mirror push failed for {identifier}: "
                              f"{status} -> {state} ({e}); the store keeps"
                              " the status and the board stays stale")
        return None
    return state


def mirror_status(conn, ticket_id, status, provider=None):
    """Move the ticket to `status` in the store, then push the move to Linear.

    The pair the loop calls at a boundary that changes ticket status, in that
    order: the store is written first because it is the truth, and Linear is
    told afterwards because it is a copy.

    Returns whether the store took the move, not what was pushed, because the
    two failures are not the same kind of thing. A push that does not land
    leaves a stale board and a `mirror_push()` warning, and the caller carries
    on regardless; a move the §3 diagram does not draw means the ticket is not
    where the caller thought it was, and only the caller knows whether its
    next step still makes sense. So the refusal is warned about, nothing is
    pushed — the board keeps showing the status the ticket actually has — and
    False says the status change did not happen.

    A storeless caller gets False for the same reason `mirror_push()` gets
    None: there is no status here to move or to project.
    """
    if conn is None:
        return False
    try:
        store.transition(conn, ticket_id, status)
    except store.IllegalTransition as e:
        warn(conn, ticket_id, f"ticket status left where it was: {e}")
        return False
    mirror_push(conn, ticket_id, provider)
    return True


# --- failure-pattern escalation ----------------------------------------------
# A rollback catches one failure; nothing here caught a *pattern*. A ticket the
# loop cannot finish stays non-terminal on the board, so the provider offers it
# again on the next pass, and the pass after that, forever — the loop has no
# memory of having already tried. The store does: one `runs` row per attempt,
# each stamped with the outcome it ended on. So the escalation is a count over
# rows that already exist, asked at the two moments that can act on it — when a
# run closes out, and before the next one is claimed.
#
# The threshold is a module constant rather than project config on purpose:
# per-project policy is a real question (a flaky integration suite deserves a
# higher bar than a typo fix) and this is not the ticket that answers it. The
# seam is here, with one name to change.
MAX_FAILED_RUNS = 2


def failure_history(conn, ticket_id):
    """The ticket's failed runs since a human last intervened, oldest first.

    One query for both halves of the escalation, as `(attempt, reason)` rows:
    its length is what trips the threshold and its reasons are what the human
    is told. Reading them together is what stops the comment from listing a
    different set of runs from the one that blocked the ticket.

    Bounded on the left by the newest `source='human'` interventions row on
    any of the ticket's runs. A recorded human action (a resume, an operator
    close-out) is a human taking the ticket back: the failures before it are
    that human's accepted history, not evidence the loop should keep
    re-parking on — so one unblock buys a fresh MAX_FAILED_RUNS rather than
    exactly one attempt forever. A board drag writes no interventions row
    and so — deliberately, per the escalation's original rule (69fe923) —
    forgives nothing; a `source='supervisor'` row grants no amnesty either
    (none is written today — the exclusion is a deliberate boundary, not a
    description of existing rows).

    Two bounds, because timestamps alone cannot draw this line. Failures are
    bounded by `endedAt` against the newest human row's `at`, so a failure
    after the human acted counts. And a failed run carrying a human
    `close_out` row of its own is excluded *by identity*: the canonical
    repair records the close-out first and releases the run a clock-read
    later, so whether that run's `endedAt` lands before or after the row's
    `at` is jitter — and a run the human has dispositioned by hand must not
    be the strike that re-parks the ticket on its next failure.

    Only `outcomeClass = 'work'` rows count. An `infra` failure — a claim
    race, a reviewer container that would not start — is the factory
    failing, not the ticket, and is neither a strike nor a line in the
    comment; `--report` still lists it.
    """
    (since,) = conn.execute(
        "SELECT COALESCE(MAX(i.at), 0) FROM interventions i"
        " JOIN runs r ON r.id = i.runId"
        " WHERE r.ticketId = ? AND i.source = 'human'",
        (ticket_id,)).fetchone()
    return conn.execute(
        "SELECT attempt, outcomeReason FROM runs r"
        " WHERE ticketId = ? AND outcome = 'failed' AND endedAt > ?"
        " AND outcomeClass = 'work'"
        " AND NOT EXISTS (SELECT 1 FROM interventions i"
        "                 WHERE i.runId = r.id AND i.source = 'human'"
        "                 AND i.\"action\" = 'close_out')"
        " ORDER BY attempt", (ticket_id, since)).fetchall()


def escalation_comment(history):
    """The Linear comment a blocked ticket gets: one line per failed run.

    The status alone says the factory gave up without saying what it kept
    hitting, and the reasons are already written — `release()` stamps each run
    with the phase it stopped in. So the comment is a rendering, not a new
    account of the failures, and a run that ended with nothing recorded says
    so rather than being left off the list.
    """
    lines = [f"**Blocked after {len(history)} failed runs.** Counted since"
             " the last recorded human intervention, if any; attempt numbers"
             " are lifetime. The factory will not claim this ticket again"
             " until a human moves it out of this state. What each counted"
             " attempt ended on:", ""]
    lines += [f"- attempt {attempt}: {reason or 'no reason recorded'}"
              for attempt, reason in history]
    return "\n".join(lines)


def escalate(conn, ticket_id, provider=None):
    """Park a ticket whose failed runs have reached `MAX_FAILED_RUNS`.

    Returns whether the ticket *is* blocked when this call returns, not
    whether this call is what blocked it. The two differ on every pass after
    the first — a ticket parked yesterday is still parked today — and the
    claim path reads the first answer, so a ticket already blocked keeps
    refusing claims instead of being worked again the moment the escalation
    stops being news.

    Only an `in_flight` ticket is escalated, which is the same rule as the
    edge the store draws. A ticket sitting anywhere else is not the loop's to
    park: `merged` work the board keeps re-offering collects failed claims
    too, and blocking that would be a lie about work that is finished — the
    stale-board re-push in `main()` is what that case wants instead.

    The block is the ordinary status move plus one comment, in that order and
    with the same discipline as `mirror_push()`: the store is the truth and is
    written first, Linear is a copy and is told after, and a comment that does
    not land is a warning on the run rather than a failure of the escalation.
    """
    if conn is None:
        return False
    row = conn.execute(
        "SELECT status, linearIssueId, linearIdentifier FROM tickets"
        " WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        return False
    status, issue_id, identifier = row
    if status == "blocked_on_operator":
        return True  # already parked; the answer, not a second escalation
    if status != "in_flight":
        return False
    history = failure_history(conn, ticket_id)
    if len(history) < MAX_FAILED_RUNS:
        return False
    if not mirror_status(conn, ticket_id, "blocked_on_operator", provider):
        return False
    # The column the schema reserves for exactly this ("set when
    # blocked_on_operator"), so a supervisor reading the store can see what is
    # being waited on without going to Linear for it.
    conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                 (f"{len(history)} runs failed on this ticket since the last"
                  " recorded human intervention and the factory stopped"
                  " claiming it; a human decides what happens next.",
                  ticket_id))
    conn.commit()
    if provider is None:
        import linear_provider as provider
    try:
        provider.comment(issue_id, escalation_comment(history))
    except Exception as e:
        warn(conn, ticket_id, f"failure history comment failed for "
                              f"{identifier} ({e}); the store keeps the block"
                              " and Linear is not told why")
    print(f"[holo2] {identifier} blocked after {len(history)} failed runs")
    return True


def close_out_failure(conn, run_id, ticket_id, reason=None, provider=None,
                      confirm=None, outcome_class="work"):
    """End a failed run the one way the factory ends failed runs.

    Three writes in a fixed order, and the order is the point. The failure
    record goes first, inside `release()`'s transaction, which stamps the
    outcome and only then clears both leases: a crash between them leaves a
    failed-looking run still holding a lease, which a human or a later release
    can free, rather than a free lease under a run that still looks alive —
    the double-claim hazard, and the one asymmetry worth ordering for. Then
    the escalation, which is a count over the row just written, so a failure
    is escalated on the pass that recorded it. Then the window, regenerated
    last so the entry that ends the run is in it.

    Factored out of the loop's `finally` because the supervisor sweep fails
    runs too, and a run failed by the sweep has to close out identically to
    one the loop failed itself — same outcome, same lease, same escalation
    counter, same rendered entry. The only thing the sweep supplies of its own
    is the `reason`, and the only thing it does differently is that the
    process being failed is not the caller.

    Which is what `confirm` is for. A caller failing somebody else's run
    decided that from a read, and between that read and this write the run's
    own process may have heartbeated, changed phase or finished — so the
    decision is re-reached here instead, inside the transaction that writes
    the failure and under the write lock that keeps the run's process out of
    it. Returning false abandons the close-out with none of it written, and
    this answers false in turn; the callback may also record what it is about
    to do -- or that it declined to -- because a note of a failure that then
    did not happen is worse than no note, and a decline nobody wrote down is
    indistinguishable from a sweep that never came. The loop's own `finally`
    passes nothing: a process failing itself cannot race itself, and there is
    no verdict of its own to re-reach.

    Only the release is under that lock. The escalation may call Linear, and
    a supervisor holding the store's write lock across a network call would
    stall every live loop for as long as the provider takes to answer — so it
    stays outside, where the failure is already committed and a push that
    fails leaves the board stale rather than the run half-closed.

    `outcome_class` is the row's `outcomeClass`: `work` unless the failure
    was an `InfraFailure`, in which case the escalation that follows does
    not count it.
    """
    with store.transaction(conn):
        if confirm is not None and not confirm():
            return False
        release_run(conn, run_id, False, reason, outcome_class)
    escalate(conn, ticket_id, provider)
    refresh_findings(conn)
    return True


def main(provider=None):
    if provider is None:
        import linear_provider as provider
    conn = open_store()
    try:
        # The provider knows its team by name rather than by id; the column's
        # contract is one row per Linear team, which the name keys just as
        # well until the provider resolves the id.
        project = store.ensure_project(conn, provider.TEAM, TARGET)
        # Startup self-sweep, read-only: it records what it saw — a first
        # strike on anything silent — so the *next* invocation or a
        # `--sweep --act` can act on the second sighting. Nothing is failed
        # from here; one sample is not evidence (STALE_STRIKES). One sweep,
        # printed once, per invocation: the refused-claim handler below
        # points back at these lines rather than re-sweeping (which would
        # count one silence twice) or reprinting (which would look like it
        # had).
        seen = sweep(conn, int(time() * 1000))
        if seen.trips or seen.watched:
            print("\n".join(sweep_lines(seen)))
            if seen.trips:
                print(SWEEP_HINT.format(target=TARGET))
        # The tickets this pass has refused to claim. A blocked ticket keeps
        # its place in the board's ready set — `blocked_on_operator` projects
        # to Todo, the column a human picks work out of — so it is offered
        # again the moment it is skipped. Remembering the refusal is what
        # turns "not this one" into "the one after it" instead of the same
        # ticket forever.
        skip = set()
        while True:
            task = provider.claim_next(skip=skip)
            if not task:
                print("[holo2] Linear has no ready tickets. done.")
                return
            # Before the lease, before the mirror: a ticket that has already
            # burned its attempts is refused here rather than claimed and then
            # discovered to be unworkable, so the escalation costs no run row
            # of its own. `escalate()` blocks it if this is the pass that
            # crossed the threshold, and says so again on every later pass —
            # which is what makes a Linear state a human dragged back to Todo
            # unable to buy the ticket another run.
            #
            # Skipped rather than stopped on, which is not the call the rest
            # of this loop makes: a stop here would be permanent. The ticket
            # sorts where it sorts and is offered first on every invocation,
            # so stopping on it would starve every ticket behind it until a
            # human noticed — and a ticket parked *for* a human is the one
            # case where there is nothing for this loop to wait on.
            # The mirror comes first, and the questions are asked of the row
            # it leaves: the live body is what the run would work from, and
            # the row a previous pass left behind can say `ready` about a
            # ticket whose criteria or verify command have since been edited
            # out. `mirror_ticket()` is an upsert with no lease, so a ticket
            # refused below has cost nothing but a refreshed row.
            ticket_id = mirror_task(conn, project, task)
            if escalate(conn, ticket_id, provider):
                print(f"[holo2] {task['id']} is blocked by repeated failures;"
                      " skipping it. a human owns it now")
                skip.add(task["id"])
                continue
            # Same place, the store's own question: §2's `pickable()`. The
            # board and the store can disagree about whether a ticket is
            # workable — a failed run leaves its mirror `in_flight` on
            # purpose, and a body edit or a hand-dragged column offers the
            # ticket again as ready — and claiming on the board's word alone
            # produced a run row that existed only to be refused by the
            # `in_flight` transition below (holophyte-bugs.md #4). Asked
            # before the claim so the refusal costs no run row and no
            # failure, and asked of the row just mirrored so an under-specced
            # body is refused whether the ticket is new or was `ready` last
            # time the store saw it: the `ready -> in_flight` move below does
            # not re-read the lists, and this is the only gate that does.
            #
            # A skipped ticket is still re-projected. The refusal used to fall
            # out of the claim, and the claim's refusal path pushed the store's
            # status back at the board as one more attempt at unsticking it —
            # a `merged` ticket whose Done push never landed is offered again
            # *because* the board is behind, and skipping it silently would
            # leave it Ready on the board for good. Best-effort, like every
            # push: a status with no board state (`needs_spec`) pushes nothing.
            verdict = store.pickable(conn, ticket_id)
            if not verdict:
                status = store_status(conn, ticket_id)
                print(f"[holo2] {task['id']} is {status} in the store,"
                      f" not claimable ({verdict.reason}); skipping it")
                mirror_push(conn, ticket_id, provider)
                skip.add(task["id"])
                continue
            try:
                run_id = store.claim(conn, project, ticket_id)
            except store.ClaimConflict as e:
                # Before any branch or worktree exists: another loop holds the
                # project, so this one stops rather than working beside it.
                # The startup sweep's sighting turns the dead end into an
                # instruction: is the holder alive, and what to type if not.
                print(f"[holo2] claim refused, not starting a run: {e}")
                if seen.trips or seen.watched:
                    print("[holo2] the sweep above shows the lease holder's"
                          " last signs of life")
                return
            # §3's `ready -> in_flight`, and the first thing the board is told
            # about this run: the claim is the moment the ticket starts being
            # worked, and the projection replaces the state call the provider
            # used to make on its own.
            if not mirror_status(conn, ticket_id, "in_flight", provider):
                # The store refused the move, so this ticket is not `ready`
                # and no work may start on it. The ordinary cause is a board
                # that is behind the store — a ticket already `merged` whose
                # Done push did not land is still non-terminal in Linear and
                # so is offered again on the next pass — and §1 is exactly
                # that the store, not the column, decides. Running anyway
                # would re-implement merged work, once per pass for as long
                # as the board stays stale.
                #
                # So: give the lease straight back, re-project the status the
                # ticket really has as one more best-effort attempt at
                # unsticking the board, and stop. Stopping rather than taking
                # the next ticket is the same call as a refused claim above —
                # store and board disagree about what is workable, and a
                # human wants to know — and it is also what keeps a stale
                # ticket the re-push cannot move (an unmapped status, a Linear
                # that is down) from being claimed round and round forever.
                refused = InfraFailure("ticket was not ready when the run"
                                       " was claimed; no work started")
                store.release(conn, run_id, "failed", str(refused),
                              outcome_class=outcome_class_of(refused))
                # This refusal is a failed run, but an `infra` one: no work
                # started, so it says nothing about the ticket and does not
                # count towards parking it. The threshold is still checked
                # here, for the `work` failures already on the ticket. A
                # ticket the escalation just parked was pushed to the board
                # by that move; the re-push is only for one that stayed where
                # it was.
                if not escalate(conn, ticket_id, provider):
                    mirror_push(conn, ticket_id, provider)
                print("[holo2] claimed ticket is not in a status work starts"
                      " from; stopping for a human")
                return
            merged = False
            reason = None
            outcome_class = "work"
            try:
                merged = run_task(task, conn, run_id, provider)
            except RunFailure as e:
                reason = str(e)
                outcome_class = outcome_class_of(e)
                print(f"[holo2] run failed: {reason}")
            except Exception as e:  # noqa: BLE001 - crash containment
                # Anything that escapes run_task is this run's failure. The
                # error text becomes the close-out reason — one clean line
                # instead of a traceback with the reason lost to
                # release_run()'s generic default (KO-146 incident, run 9).
                # Collapsed to one line: sh()'s message carries the failed
                # command's whole output, and the reason lands verbatim in an
                # escalation comment's markdown bullet.
                reason = " ".join(f"{type(e).__name__}: {e}".split())
                print(f"[holo2] run crashed: {reason}")
            finally:
                if merged:
                    release_run(conn, run_id, merged)
                    # `in_flight -> merged`, projected as Done. A run that did
                    # not merge leaves the ticket in flight on purpose: the
                    # branch is preserved for a human and the board should go
                    # on saying the work is open, so there is nothing to push.
                    mirror_status(conn, ticket_id, "merged", provider)
                    # Close-out, and the first moment the run's own outcome is
                    # a row: the window is regenerated here rather than inside
                    # `run_task()` so the entry that ends the run is in it.
                    refresh_findings(conn)
                else:
                    # The failure close-out: release, escalate if this failure
                    # was one too many, regenerate the window. Shared with the
                    # supervisor sweep, which fails runs this loop is no
                    # longer around to fail itself. Its own failure (a locked
                    # store, say) must not replace what was in flight — a
                    # KeyboardInterrupt included — with a traceback of its
                    # own; the lease stays for release() or the sweep.
                    try:
                        close_out_failure(conn, run_id, ticket_id, reason,
                                          provider=provider,
                                          outcome_class=outcome_class)
                    except Exception as close_err:  # noqa: BLE001
                        print(f"[holo2] close-out failed: {close_err}")
            if not merged:
                # The regenerated window stays uncommitted, like the preserved
                # branch it describes: a human closes both out. Nonzero so the
                # shell — and anything supervising it — sees the failure.
                return 1  # stop on first failure; ticket stays In Progress
            commit_findings(f"Complete task {task['id']}: {task['title']}")
    finally:
        conn.close()


# --- estimate vs actual ------------------------------------------------------
# The rows already carry every number a burndown needs: when a run started and
# ended, the estimate it was claimed under, how many rounds it took. So the
# report is a query and an aligned print rather than a grep over FINDINGS.md --
# the ledger line was the only reading of this data until now, and a rendering
# of the newest 25 entries is not something a calibration question can be
# asked of. Nothing here writes, claims or calls Linear.
REPORT_HEADERS = ("ticket", "actual", "estimate", "ratio", "rounds", "outcome")
REPORT_GAP = "  "


def report_rows(conn):
    """Every ended run, oldest first, as the report's own tuple.

    `(ticket, actual_min, estimate_min, ratio, rounds, outcome)`, with
    `estimate` and `ratio` None when the run was claimed against no estimate
    -- an older run, or a ticket Linear gave no points. None rather than zero
    because "not comparable" is not a ratio of nothing, and the summary below
    leaves those runs out of its averages instead of dragging them to 0.
    """
    rows = []
    for ticket, started, ended, time_box, rounds, outcome in conn.execute(
            "SELECT t.linearIdentifier, r.startedAt, r.endedAt, r.timeBoxMs,"
            " r.reviewRoundCount, r.outcome"
            " FROM runs r JOIN tickets t ON t.id = r.ticketId"
            " WHERE r.endedAt IS NOT NULL"
            " ORDER BY r.endedAt, r.id").fetchall():
        actual = (ended - started) / 60000
        estimate = time_box / 60000 if time_box else None
        rows.append((ticket, actual, estimate,
                     actual / estimate if estimate else None,
                     rounds, outcome or "ended"))
    return rows


def report_summary(rows):
    """The last line: how many runs, and how the ratios sit.

    Mean and median together, because they answer different halves of the
    calibration question -- the mean carries the one run that blew its budget,
    the median says what a typical ticket costs -- and the early signal worth
    watching (actuals running several times under estimate) is a claim about
    the middle, not about the total.
    """
    ratios = [row[3] for row in rows if row[3] is not None]
    if not ratios:
        return f"{len(rows)} runs · no estimates to compare against"
    counted = (f"{len(rows)} runs" if len(ratios) == len(rows)
               else f"{len(rows)} runs · {len(ratios)} with an estimate")
    return (f"{counted} · mean ratio {statistics.fmean(ratios):.2f}"
            f" · median ratio {statistics.median(ratios):.2f}")


def report_lines(conn):
    """The whole report as lines: a header, one line per ended run, a summary.

    Columns are padded to the widest cell in them so the numbers line up in a
    terminal; the ticket and the outcome read left, everything numeric reads
    right. A store with no ended run says so rather than printing a header
    over nothing.
    """
    rows = report_rows(conn)
    if not rows:
        return ["no completed runs yet"]
    table = [REPORT_HEADERS]
    for ticket, actual, estimate, ratio, rounds, outcome in rows:
        table.append((
            ticket,
            f"{actual:.1f}",
            f"{estimate:.0f}" if estimate is not None else "n/a",
            f"{ratio:.2f}" if ratio is not None else "n/a",
            str(rounds),
            outcome,
        ))
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [
        REPORT_GAP.join(
            cell.ljust(width) if i in (0, len(widths) - 1) else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))).rstrip()
        for row in table
    ]
    return lines + [report_summary(rows)]


def report(conn=None, out=None):
    """Print the target store's estimate-vs-actual table. Returns nothing.

    `--report`'s whole body: it reads rows and prints them, so no ticket is
    claimed, no worktree is cut and no provider is imported -- which is what
    makes it safe to run against the store of a loop that is still working.

    The one write it can make is `open_store()`'s migration: a store older
    than the run row's estimate column is brought up to the schema this
    queries instead of failing on the missing column, and the round counts an
    older module never stamped are recomputed from the rounds themselves
    rather than reported as zero. A target with no store at all is not created
    for the sake of an empty table; it is reported.
    """
    out = out or sys.stdout
    if conn is None and not STORE_PATH.exists():
        print(f"[holo2] no store at {STORE_PATH}", file=out)
        return
    owned = conn is None
    conn = conn if conn is not None else open_store()
    try:
        print("\n".join(report_lines(conn)), file=out)
    finally:
        if owned:
            conn.close()


# --- the supervisor's stale-run sweep -----------------------------------------
# The loop watches itself only while it is alive. A run whose process crashed,
# hung, or was killed leaves a row in a work phase, a heartbeat that stopped
# and a project lease nobody will ever give back -- and nothing noticed.
#
# The sweep is the noticing: it reads runs, counts strikes and reports what
# tripped. `--sweep` on its own stops there, which is what makes it safe to
# point at a loop that is still working. `--sweep --act` goes on to do
# something about each trip, and what it does is the loop's own failure
# close-out (`close_out_failure()`) run from outside the run: the run is
# failed, its leases are given back, the failure counts towards the ticket's
# escalation threshold, and the window is regenerated. Nothing is killed and
# nothing is deleted -- the branch and worktree wait for a human exactly as
# they do after a failure the loop noticed itself. Detection without action
# still needs somebody watching; action is what makes an overnight run safe,
# because a hung run becomes a clean failure the next invocation can route
# around instead of a zombie holding the lease forever.

# How old a heartbeat has to be before a sighting counts as silent, and how
# many consecutive silent sightings trip the run. Two, from the v1 TUI mining:
# one sample false-positives on a load spike, and a supervisor that kills live
# runs is worse than one that notices a dead one a minute late.
HEARTBEAT_STALE_MS = 5 * 60 * 1000
STALE_STRIKES = 2
# How far past its claim-time estimate a run may run before the time box is
# considered blown. Generous on purpose: the estimate is a 15-30 minute
# guess, and the trip is meant to catch a run that is not going to finish
# rather than one that is merely slower than the ticket hoped.
BUDGET_GRACE = 1.5
# How much of their findings two consecutive review rounds may share before
# the review is read as circling rather than converging: the Jaccard overlap
# `store.findings_overlap()` measures, over the `(path, line, severity)` keys
# the fingerprint hashes. Half, because a fix round that leaves half of the
# reviewer's complaints standing has not moved the review, and the round after
# it is the terminal adjudication -- a doomed one is cheaper failed now than
# paid for. Two rounds are compared and never one: a healthy run sits in
# `reviewing` with a single round on file, and there is nothing to compare
# it against.
REVIEW_OVERLAP_THRESHOLD = 0.5
# How long the supervisor sleeps between two acting sweeps. A minute: fine
# enough that a dead run is noticed within `HEARTBEAT_STALE_MS` plus one
# interval of dying, coarse enough that the store's write lock is taken for
# the sweep's arithmetic sixty times an hour and not six hundred.
SUPERVISE_INTERVAL_SEC = 60
# The phases the review-stuck check applies in: the ones a run is in between
# a review round ending and the next one starting. Anywhere else the rounds on
# file are history the run has moved past, not a review it is still inside.
REVIEW_PHASES = ("reviewing", "addressing")

# The five knobs above have an address: the optional `[supervisor]` table of
# `<repo>.holophyte.toml`. Different targets legitimately want different
# patience -- a Go build's setup is slower than stdlib Python's -- and the
# constants are the defaults, not the lookup sites: an absent table is
# exactly the numbers above. The keys are named in the units an operator
# thinks in (minutes, seconds, a multiplier, a fraction) and `sweep_config()`
# converts them to the units the sweep computes in.
SUPERVISOR_KEYS = {
    "heartbeat_stale_min": HEARTBEAT_STALE_MS / 60000,
    "stale_strikes": STALE_STRIKES,
    "budget_grace": BUDGET_GRACE,
    "review_overlap_threshold": REVIEW_OVERLAP_THRESHOLD,
    "sweep_interval_sec": SUPERVISE_INTERVAL_SEC,
}
# The knobs as the sweep reads them: the same five, with the heartbeat
# threshold already in milliseconds, so the arithmetic in `sweep()` is the
# arithmetic it always was.
KNOWN_KEYS["supervisor"] = frozenset(SUPERVISOR_KEYS)
SweepConfig = collections.namedtuple(
    "SweepConfig",
    ("heartbeat_stale_ms", "stale_strikes", "budget_grace",
     "review_overlap_threshold", "sweep_interval_sec"))


def sweep_config():
    """The target's sweep thresholds: `[supervisor]` over the defaults.

    Every key is optional and an absent table is the module constants exactly.
    A key that is present is checked here, the way `agent_command()` checks a
    route: a threshold is a number, thresholds and intervals are positive,
    the strike requirement is a whole number of sightings, and the overlap is
    a fraction in (0, 1] -- a share of findings above one is unreachable, and
    a share of zero trips every review that found anything at all. A value
    outside its constraint is a startup error naming the key and the
    constraint, like malformed TOML: a negative threshold the factory quietly
    replaced with its default would sweep with numbers nobody chose. Booleans
    are refused as numbers, because `true` is a 1 TOML never meant, and so
    are `inf` and `nan`, which TOML also spells: an infinite threshold is a
    trip that silently never fires, and an infinite interval is a `sleep()`
    that raises OverflowError instead of sleeping.

    Keys the table names that this version does not know are refused by
    `check_config_keys()`, which startup runs beside this.
    """
    table = config().get("supervisor", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {CONFIG_PATH}: [supervisor] must be a table, got "
            f"{type(table).__name__}")
    values = {}
    for key, default in SUPERVISOR_KEYS.items():
        value = table.get(key, default)
        number = (isinstance(value, (int, float))
                  and not isinstance(value, bool) and math.isfinite(value))
        if key == "stale_strikes":
            constraint, ok = "a positive integer", number and (
                isinstance(value, int) and value > 0)
        elif key == "review_overlap_threshold":
            constraint, ok = "a number in (0, 1]", number and 0 < value <= 1
        else:
            constraint, ok = "a finite positive number", number and value > 0
        if not ok:
            raise SystemExit(
                f"[holo2] {CONFIG_PATH}: [supervisor] {key} must be "
                f"{constraint}, got {value!r}")
        values[key] = value
    return SweepConfig(
        heartbeat_stale_ms=values["heartbeat_stale_min"] * 60000,
        stale_strikes=values["stale_strikes"],
        budget_grace=values["budget_grace"],
        review_overlap_threshold=values["review_overlap_threshold"],
        sweep_interval_sec=values["sweep_interval_sec"])

# The phases a run can be swept in: everything the store's enum has, less the
# three a finished run sits in and `blocked_on_operator`. Derived from
# `store.PHASES` rather than listed, so a phase added there is swept by
# default -- the safe direction for a check whose failure mode is a hung run
# nobody looks at.
#
# `blocked_on_operator` is excluded because a parked run is *supposed* to have
# no heartbeat: the loop wrote the question, released the process and went
# home, and the run waits for a human for however long that takes. Sweeping it
# would report every parked run as dead within five minutes, and 2/5 would
# then fail the one state the design keeps open for an operator's answer.
SWEEPABLE_PHASES = tuple(
    phase for phase in store.PHASES
    if phase not in store.ENDED_PHASES and phase != "blocked_on_operator")

# The mechanical conditions a run can trip. `time_box` is spelled as the
# `interventions.trigger` value of the same name, so the name an operator
# reads here is the name that vocabulary already uses.
STALE_HEARTBEAT = "stale_heartbeat"
TIME_BOX = "time_box"
REVIEW_STUCK = "review_stuck"

# The `runEvents.kind` an acted-on trip is recorded under, so the condition
# that failed a run is in the run's own stream and not only in its outcome
# reason: a reader following the narrative sees the supervisor arrive.
SWEEP_EVENT = "supervisor_sweep"

# One tripped run, as the sweep reports it: which run, whose ticket, what it
# was doing, which condition, and the numbers that condition was decided on.
# `evidence` is prose for an operator, not a parseable field -- what a reader
# needs to agree with the verdict without opening the database.
#
# `heartbeat` is not for the report. It is the `lastHeartbeat` the verdict was
# reached on, carried so `still_tripped()` can ask whether the run has shown
# any sign of life since -- a verdict is only actionable against the state it
# was made from.
Trip = collections.namedtuple(
    "Trip",
    ("run_id", "ticket", "phase", "condition", "evidence", "heartbeat"))
# What one pass found: how many live runs it looked at, the trips among them,
# whether it acted on them, and the runs it is watching -- silent, at a strike
# below the trip threshold. The count is carried because "nothing tripped" is
# only reassuring next to the number of runs that were checked -- silence and
# health look identical without it -- and `acted` because a report of tripped
# runs reads completely differently depending on whether they were left alone
# or failed. `watched` is carried because a first strike printed as "all
# healthy" hides exactly the evidence the next invocation acts on.
Sweep = collections.namedtuple("Sweep",
                               ("swept", "trips", "acted", "watched",
                                "outcomes"))
# What acting on one trip came to. `acted` is whether the run was failed;
# `phase` is the run's phase as the re-check found it, which for a decline is
# the status the summary names -- the run finished, moved on or answered --
# and for an act is the phase it was failed in. `acted` is the outcome, not
# the flag `sweep()` was called with: the two parted in holophyte-bugs.md #1,
# where a summary read the flag and reported a failure the re-check had
# refused to write.
Outcome = collections.namedtuple("Outcome", ("trip", "acted", "phase"))


def review_overlap(conn, run_id):
    """How much `run_id`'s latest two finished review rounds share, or None.

    `(earlier_round, later_round, overlap)` from `store.findings_overlap()`
    over the two most recent rounds with an `endedAt` -- a round still being
    reviewed has no findings to compare yet. None when there are fewer than
    two such rounds, or when either round found nothing: two empty rounds
    score 1.0 by the measure's definition (equal sets), but a `pass` after a
    `pass`, or a round after an approval, is a review that has nothing left
    to say rather than one repeating itself, and reading the sentinel
    fingerprint as overlap would trip every run whose review went well.

    The findings are the store's own JSON, written by
    `store.record_review_round()` after it validated them, so a row that
    fails to compare here is a corrupted store rather than a reviewer's bad
    day -- and the `ValueError` is left to surface as one.
    """
    rounds = conn.execute(
        "SELECT round, findings FROM reviewRounds"
        " WHERE runId = ? AND endedAt IS NOT NULL"
        " ORDER BY round DESC LIMIT 2", (run_id,)).fetchall()
    if len(rounds) < 2:
        return None
    (later, later_findings), (earlier, earlier_findings) = rounds
    earlier_findings = json.loads(earlier_findings)
    later_findings = json.loads(later_findings)
    if not earlier_findings or not later_findings:
        return None
    return earlier, later, store.findings_overlap(earlier_findings,
                                                  later_findings)


def still_tripped(conn, trip, knobs=None):
    """Does `trip`'s verdict still hold of the run it was reached on?

    Asked again at the moment of acting, under the write lock, because the
    classification that produced the trip committed and let the run's own
    process back in. What that process may have done since is the whole
    question: a run that ended is already closed out and must not be re-ended
    over the top of its real outcome, and a run that moved on is doing
    something and can wait for the sweep after this one -- the tally that
    tripped it survives, so a run that is really gone trips again a minute
    later, which is a cheap price for never failing a live one.

    A stale heartbeat asks one thing more: that `lastHeartbeat` is still the
    timestamp the verdict was read from. Any beat at all is the run answering
    the only question the condition asked, and a run that answered is alive
    however long it was quiet before. A blown time box asks the opposite --
    an overrunning run heartbeats, that is what makes it an overrun rather
    than a death -- so a fresh beat is no acquittal there and is not treated
    as one. A stuck review is alive too, so its heartbeat says nothing; what
    it asks instead is that the overlap still holds, recomputed over whatever
    rounds are on file now. The phase alone cannot tell: a run that went
    through `addressing` and back has a new finished round and the phase the
    verdict named, and if that round cleared the reviewer's complaints the
    review has moved and the run is acquitted. If it repeats them, the run
    is the same stuck review with one more round on file, and the verdict
    stands even though the rounds it now rests on are later than the ones
    the evidence names. And a run that left the phase and came back with no
    new round -- through `addressing` and `verifying` into its terminal
    adjudication -- is exactly the run the condition names: the adjudication
    is what the trip is meant to spare paying for, and a sweep arriving
    fresh at that moment would trip it on the same two rounds.

    `knobs` is the `SweepConfig` the verdict was reached under, so the
    overlap is re-asked against the threshold that tripped it.
    """
    knobs = sweep_config() if knobs is None else knobs
    row = conn.execute(
        "SELECT endedAt, phase, lastHeartbeat FROM runs WHERE id = ?",
        (trip.run_id,)).fetchone()
    if row is None:
        return False
    ended_at, phase, heartbeat = row
    if ended_at is not None or phase != trip.phase:
        return False
    if trip.condition == STALE_HEARTBEAT:
        return heartbeat == trip.heartbeat
    if trip.condition == REVIEW_STUCK:
        overlap = review_overlap(conn, trip.run_id)
        return (overlap is not None
                and overlap[2] >= knobs.review_overlap_threshold)
    return True


def act_on_trip(conn, trip, provider=None, knobs=None):
    """Fail one tripped run, if it is still tripped; return an `Outcome`.

    The whole of what acting means. `close_out_failure()` is the loop's own,
    unchanged and not re-implemented here, so a swept failure is the same kind
    of row as any other failure: the same outcome, the same released leases,
    the same contribution to the ticket's escalation count, the same rendered
    entry. Only two things are the sweep's own -- the reason, which names the
    condition instead of the phase, and the event, which puts the supervisor's
    arrival in the run's narrative where the reason alone would leave the
    stream ending at whatever the dead process last managed to say.

    Both go in under `close_out_failure()`'s `confirm`, which is to say inside
    the transaction that writes the failure, and only once `still_tripped()`
    has agreed the verdict survives. The classification pass had to commit
    before this ran -- failing a run may call Linear, and the store's write
    lock must not be held across a network call -- and committing let the
    run's process back in to heartbeat or finish. Re-checking there and
    failing there is what keeps the two from separating: a run cannot prove
    itself alive in the gap between being confirmed dead and having its lease
    handed to the next worker, because under one `BEGIN IMMEDIATE` there is no
    gap for it to do so in.

    A decline is recorded too, under the same event kind: the supervisor
    looked, found the run finished or moved on, and stood down. Without the
    row a reader of the run's stream cannot tell a sweep that declined from
    one that never arrived, and the summary line the operator reads is
    derived from this same answer -- `acted` here is what happened, never
    the flag the sweep was called with.

    Nothing is signalled, killed or deleted. Freeing the lease and recording
    the failure is enough to unblock the queue, and a supervisor that also
    tried to kill things would need to be right about which process it was
    killing. A wedged run that is not writing to the store but is still
    working on disk therefore remains out of scope, and is the reason the
    strike rule is two sightings rather than one.
    """
    knobs = sweep_config() if knobs is None else knobs
    (ticket_id,) = conn.execute(
        "SELECT ticketId FROM runs WHERE id = ?", (trip.run_id,)).fetchone()
    seen = {"phase": None}

    def confirm():
        # Read under the same lock the verdict is re-reached under, so the
        # phase the outcome names is the one the decision was made on.
        row = conn.execute(
            "SELECT phase FROM runs WHERE id = ?", (trip.run_id,)).fetchone()
        seen["phase"] = row[0] if row is not None else None
        if not still_tripped(conn, trip, knobs):
            if row is not None:
                store.record_event(
                    conn, trip.run_id, SWEEP_EVENT,
                    f"supervisor sweep: {trip.condition} ({trip.evidence})"
                    f" no longer held at re-check; run is now {row[0]};"
                    " no action")
            return False
        store.record_event(
            conn, trip.run_id, SWEEP_EVENT,
            f"supervisor sweep: {trip.condition} ({trip.evidence});"
            " failing the run and releasing its leases")
        return True

    acted = close_out_failure(
        conn, trip.run_id, ticket_id,
        f"swept by the supervisor in phase {trip.phase}: {trip.condition}"
        f" ({trip.evidence}); branch and worktree preserved for a human",
        provider, confirm)
    return Outcome(trip, acted, seen["phase"])


def sweep(conn, now, act=False, provider=None, knobs=None):
    """Check every live run for a tripped condition; return a `Sweep`.

    `now` is epoch milliseconds and is a parameter, not a clock read: every
    threshold here is an age, and a test that has to sleep to make a run look
    stale is a test that is slow and flaky in exchange for nothing.

    Each swept run is first *sighted*: silent or not, the observation is
    counted in the store, because two consecutive silent sightings are what a
    stale-heartbeat trip is made of and a run seen alive has to clear the
    count it had. The heartbeat goes with the verdict, so a run that answered
    between two sweeps and fell quiet again starts its tally over even though
    no sweep caught it awake. Without `act`, that bookkeeping is the only
    write this makes -- no phase moves, no lease is freed, no ticket is
    touched -- which is what makes a bare `--sweep` safe against a working
    loop. With `act`, every trip is then put to `act_on_trip()`, and what
    each came to -- failed, or declined because the run had moved -- is
    carried as the sweep's `outcomes`, one per trip in the same order.

    A run reports at most one trip, and the conditions are asked in the order
    of what they explain. A stale heartbeat comes first: a dead worker
    explains an overrun and a stalled review both, while neither says
    anything about whether a process is still running. A blown time box
    comes before a stuck review because it is the older and broader budget,
    and a stuck review -- the latest two finished rounds of a run in
    `REVIEW_PHASES` sharing the overlap threshold or more of their
    findings -- is the narrowest: a live run inside its budget whose fix
    round left the reviewer's complaints standing.

    Sightings have a minimum spacing: a silent run whose strike on file is
    younger than the stale threshold is not struck again — the tally it has
    is used as it stands. Two sweeps seconds apart (an operator relaunching,
    a launch straight after a --sweep) are one observation of one silence,
    and counting them separately would let two launches in a minute
    manufacture the second strike the two-strike rule exists to demand of
    two separate silences.

    The whole pass is one `store.transaction()`, because the loop it watches
    is a different process writing the very columns this reads. Classifying
    from a snapshot and then striking in a second transaction leaves a gap in
    which the run heartbeats or finishes, and the strike lands on a state that
    no longer holds -- a live run one sighting nearer a trip it does not
    deserve, or a run that ended a millisecond ago reported as dead. Under one
    `BEGIN IMMEDIATE` the read the verdict is made from and the write it is
    recorded in are the same instant, and a heartbeat arriving mid-sweep waits
    and lands cleanly on the next one. The block is arithmetic over the live
    runs and nothing else, so the loop is held up for no longer than that --
    which is also why acting happens after it has committed rather than
    inside it: failing a run releases leases, escalates a ticket and may call
    Linear, and holding the store's write lock across a network call would
    stall every live loop for as long as the provider takes to answer.

    `knobs` is the target's `SweepConfig`; the default is `sweep_config()`,
    the `[supervisor]` table over the module constants.
    """
    knobs = sweep_config() if knobs is None else knobs
    stale_ms, strikes_needed = knobs.heartbeat_stale_ms, knobs.stale_strikes
    grace, overlap_threshold = knobs.budget_grace, knobs.review_overlap_threshold
    trips, watched = [], []
    with store.transaction(conn):
        swept = conn.execute(
            "SELECT r.id, t.linearIdentifier, r.phase, r.lastHeartbeat,"
            " r.startedAt, r.timeBoxMs"
            " FROM runs r JOIN tickets t ON t.id = r.ticketId"
            " WHERE r.endedAt IS NULL"
            f"   AND r.phase IN ({', '.join('?' * len(SWEEPABLE_PHASES))})"
            " ORDER BY r.id", SWEEPABLE_PHASES).fetchall()
        for run_id, ticket, phase, heartbeat, started, time_box in swept:
            silent = now - heartbeat
            stale = silent > stale_ms
            on_file = conn.execute(
                "SELECT strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
                (run_id,)).fetchone()
            if (stale and on_file is not None and heartbeat <= on_file[1]
                    and now - on_file[1] < stale_ms):
                # A sighting within one stale-threshold of the last is the
                # same sample: two launches seconds apart must not
                # manufacture the second strike the two-strike rule exists
                # to require of two separate silences. The tally stands.
                strikes = on_file[0]
            else:
                strikes = store.record_strike(
                    conn, run_id, stale, heartbeat, now)
            elapsed = now - started
            if strikes >= strikes_needed:
                trips.append(Trip(
                    run_id, ticket, phase, STALE_HEARTBEAT,
                    f"silent for {silent / 60000:.1f} min"
                    f" over {strikes} consecutive sweeps", heartbeat))
            elif time_box and elapsed > time_box * grace:
                trips.append(Trip(
                    run_id, ticket, phase, TIME_BOX,
                    f"{elapsed / 60000:.1f} min against a"
                    f" {time_box / 60000:.0f} min box ({grace}x grace)",
                    heartbeat))
            elif (phase in REVIEW_PHASES
                    and (overlap := review_overlap(conn, run_id)) is not None
                    and overlap[2] >= overlap_threshold):
                earlier, later, shared = overlap
                trips.append(Trip(
                    run_id, ticket, phase, REVIEW_STUCK,
                    f"rounds {earlier} and {later} share {shared:.2f} of"
                    f" their findings ({overlap_threshold} threshold)",
                    heartbeat))
            elif strikes:
                # Silent, but one sighting short of a trip: not evidence yet,
                # and not "all healthy" either. Carried for rendering so the
                # operator sees what the next sweep can act on.
                watched.append(
                    f"run {run_id} ({ticket}, {phase}): silent"
                    f" {silent / 60000:.1f} min, strike {strikes} of"
                    f" {strikes_needed}")
    outcomes = []
    if act:
        outcomes = [act_on_trip(conn, trip, provider, knobs) for trip in trips]
    return Sweep(len(swept), trips, act, tuple(watched), tuple(outcomes))


SWEEP_HEADERS = ("ticket", "run", "phase", "condition", "evidence")

# Printed under a table with trips in it wherever the reader is an operator
# who did not ask for a sweep (startup, a refused claim): the table says what
# is wrong, this says what to type. `{target}` is filled at the print site so
# the line is copy-pasteable for a non-default target.
SWEEP_HINT = ("[holo2] tripped runs are failed by"
              " `factory.py {target} --sweep --act`;"
              " a bare --sweep re-checks first")


def _runs(n):
    """`n` runs, counted in English -- the summary line reads as a sentence."""
    return "1 run" if n == 1 else f"{n} runs"


def sweep_lines(result):
    """The sweep as lines: a header, one line per trip, a summary.

    A clean sweep prints what it checked rather than nothing. Empty output is
    ambiguous -- it reads the same as a crashed supervisor, a mistyped target
    or a store with no runs in it -- so the quiet case is an assertion an
    operator can act on, and the three quiet cases say which one they are.

    An acting sweep adds one outcome line per trip and a summary that counts
    the failed apart from the declined. Both come from `Outcome`, which is
    what `act_on_trip()` actually did, and never from the `acted` flag the
    sweep was called with: a re-check that stood down because the run had
    finished is reported as exactly that, naming the status it found, and
    the words "failed and leases released" are printed only for a run whose
    failure was written. A read-only sweep has no outcomes and prints as it
    always has.
    """
    if not result.swept:
        return ["no runs in flight, nothing to sweep"]
    if not result.trips:
        # A first-strike sighting must not read as health: "none tripped"
        # plus the watched lines is the honest quiet case.
        if result.watched:
            return [f"{_runs(result.swept)} swept, none tripped",
                    *result.watched]
        return [f"{_runs(result.swept)} swept, all healthy"]
    table = [SWEEP_HEADERS]
    table += [(trip.ticket, f"run {trip.run_id}", trip.phase, trip.condition,
               trip.evidence) for trip in result.trips]
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [
        REPORT_GAP.join(cell.ljust(width)
                        for cell, width in zip(row, widths)).rstrip()
        for row in table
    ]
    for outcome in result.outcomes:
        run_id = outcome.trip.run_id
        if outcome.acted:
            lines.append(f"acted: failed run {run_id}, leases released")
        else:
            status = ("gone" if outcome.phase is None
                      else f"now {outcome.phase}")
            lines.append(f"declined: run {run_id} is {status}; no action")
    lines += list(result.watched)
    failed = sum(1 for outcome in result.outcomes if outcome.acted)
    declined = len(result.outcomes) - failed
    summary = f"{len(result.trips)} tripped of {_runs(result.swept)} swept"
    if failed:
        summary += f", {failed} failed and leases released"
    if declined:
        summary += f", {declined} declined, no action"
    return lines + [summary]


def sweep_report(conn=None, now=None, out=None, act=False, provider=None):
    """Print the target store's tripped runs, failing them when `act`.

    `--sweep`'s whole body, and a sibling of `report()` in what it refuses to
    do: no ticket is claimed and no worktree is cut, so it is safe to run
    against the store of a loop that is still working -- the case it exists
    for. Unlike `report()` it does write, to exactly one table: the strike
    tally `sweep()` keeps, without which "two consecutive sweeps" could not
    span two invocations.

    `act` is what `--act` adds, and it adds it to nothing else: a pass that
    trips no run writes exactly what a read-only pass writes, so acting costs
    nothing on the sweeps that find everything healthy. A pass that does trip
    something fails those runs, and only then is a provider needed -- and only
    if a ticket has reached its escalation threshold.

    The table is printed after the acting rather than before it, so it is a
    record of what happened rather than a promise: a best-effort push that
    warns on its way past appears above the summary claiming the runs were
    failed, not below it.

    A target with no store has no runs to sweep and is reported rather than
    created, the way `--report` answers the same mistake.
    """
    out = out or sys.stdout
    if conn is None and not STORE_PATH.exists():
        print(f"[holo2] no store at {STORE_PATH}", file=out)
        return
    owned = conn is None
    conn = conn if conn is not None else open_store()
    try:
        if now is None:
            now = int(time() * 1000)
        print("\n".join(sweep_lines(sweep(conn, now, act, provider))), file=out)
    finally:
        if owned:
            conn.close()


# --- the supervisor loop --------------------------------------------------------
# A sweep only helps if something runs it. `--supervise` is the something: one
# process per target that runs the acting sweep, sleeps, and runs it again
# until a signal tells it to stop -- the smallest thing that makes "the
# factory runs overnight and the supervisor watches" true.
#
# One per target, because two supervisors sweeping one store would each take
# their own sighting of every silence and manufacture between them the second
# strike the two-strike rule exists to demand of two separate silences. The
# arbitration is a lockfile beside the store, taken with an exclusive create
# (v1 TUI mining, server.ts:111-160): create-then-check, never check-then-
# create, because the gap between a check and a create is exactly where a
# second starter slips through. A lock that exists is then read: a live pid
# means a rival and this starter aborts naming it; a dead pid is a supervisor
# that crashed without cleaning up, and its lock is reclaimed; a lock that
# says neither -- empty, half-written, not ours to parse -- is ambiguous, and
# an ambiguous probe never spawns a rival. It aborts and says what it saw.

# The interval between two acting sweeps is `SUPERVISE_INTERVAL_SEC`, or the
# target's `[supervisor] sweep_interval_sec`, read with the other thresholds
# by `sweep_config()`.
# The signals a supervisor stops on. Both mean the same thing here -- finish
# the pass in hand, give the lock back, exit clean -- because an operator's
# Ctrl-C and a service manager's stop are the same request.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def supervisor_lock_path(target=None):
    """The lockfile for `target`'s supervisor, in its state directory.

    Beside the store rather than inside the target for the store's own
    reason: nothing about the target checkout should have to know the factory
    exists, and a lock inside it is dirt a task's `git add -A` could commit.
    Derived through `state_dir()` so the lock cannot end up addressing a
    different directory from the store it guards.
    """
    return state_dir(TARGET if target is None else target) / "supervisor.lock"


class SupervisorHeld(Exception):
    """The target already has a supervisor, or a lock this one will not take.

    `pid` is the live holder when there is one, and None when the lock could
    not be read -- the two cases the message tells apart, because the first
    is answered by doing nothing and the second by an operator looking at the
    file.
    """

    def __init__(self, path, pid=None, started_at=None):
        self.path, self.pid, self.started_at = path, pid, started_at
        if pid is None:
            what = (f"[holo2] supervisor lock {path} exists but names no"
                    " process; refusing to guess. remove it if no supervisor"
                    " is running")
        else:
            since = (f" since {started_at}" if started_at is not None else "")
            what = (f"[holo2] a supervisor is already running for {TARGET}:"
                    f" pid {pid}{since} holds {path}; not starting another")
        super().__init__(what)


def pid_alive(pid):
    """Whether `pid` names a process that exists, by asking the kernel.

    Signal 0 delivers nothing and answers only whether it could have: a
    process that is gone is ESRCH, one that belongs to someone else is EPERM
    -- and EPERM is still alive, which is the answer that matters here.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_supervisor_lock(path):
    """The `(pid, started_at)` a lockfile names, or None if it names none.

    Written as two integers on one line by `acquire_supervisor_lock()`;
    anything else -- an empty file a crashed starter left between its create
    and its write, a file somebody else wrote -- is None, and the caller
    treats None as a lock it must not remove.
    """
    try:
        fields = Path(path).read_text().split()
        pid, started_at = int(fields[0]), int(fields[1])
    except (OSError, ValueError, IndexError):
        return None
    return pid, started_at


@contextlib.contextmanager
def reclaim_turn(path):
    """Hold the reclaim sidecar of the lock at `path` for the block's span.

    A blocking flock on `<path>.reclaim`, which is never unlinked so every
    starter locks the same inode. It orders reclaims only; the lock itself
    stays the exclusive create, so a starter that never has to reclaim never
    touches the sidecar.
    """
    fd = os.open(f"{path}.reclaim", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def acquire_supervisor_lock(path, pid=None, now=None):
    """Take the supervisor lock at `path` for `pid`; raise `SupervisorHeld`.

    The exclusive create is the arbitration: two starters racing here both
    reach the kernel, and the kernel lets one of them through. The loser
    reads the lock the winner wrote and finds a live pid. A lock left by a
    dead supervisor is reclaimed -- unlinked, then created again through the
    same exclusive door, so a reclaim that loses a race to another starter
    loses the way any second starter does. The reclaim itself -- read the
    holder, judge it dead, unlink -- runs under an flock on a sidecar beside
    the lock, so two starters that both read the same dead pid take turns:
    the second finds, once its turn comes, the live lock the first has just
    written, and is refused by it. (An inode compared before the unlink is
    not that guard: between the comparison and the unlink a rival can have
    reclaimed and re-created the file, and the unlink then takes the rival's
    live lock.) A lock naming this very pid is the stale case too: a
    supervisor is not its own rival, and a pid comes round again. Only one
    reclaim is attempted, because a create that fails after it is a starter
    that never needed a turn, not a second stale lock.
    """
    path = Path(path)
    pid = os.getpid() if pid is None else pid
    now = int(time() * 1000) if now is None else now
    # The target's state directory, on first need: a supervisor can be the
    # first thing to run against a target.
    path.parent.mkdir(parents=True, exist_ok=True)

    def created():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{pid} {now}\n")
        return True

    if created():
        return path
    with reclaim_turn(path):
        holder = read_supervisor_lock(path)
        if holder is None and path.exists():
            raise SupervisorHeld(path)
        if holder is not None:
            if holder[0] != pid and pid_alive(holder[0]):
                raise SupervisorHeld(path, *holder)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        # Created (and written) before the turn is given up, so the starter
        # waiting for it reads a whole lock, never the empty file between a
        # create and its write.
        if created():
            return path
    holder = read_supervisor_lock(path)
    raise SupervisorHeld(path, *(holder or ()))


def release_supervisor_lock(path, pid=None):
    """Remove the lock at `path` if it is `pid`'s; leave anyone else's alone.

    Checked before it is removed because the lock may not be ours any more: a
    supervisor that was wrongly judged dead has had its lock reclaimed, and
    removing the reclaimer's lock on the way out would let a third starter
    in beside it.
    """
    pid = os.getpid() if pid is None else pid
    holder = read_supervisor_lock(path)
    if holder is not None and holder[0] == pid:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def supervise_pass(pid, started_at, now=None, provider=None, out=None):
    """One pass: an acting sweep of the target's store, then a heartbeat.

    The store is opened and closed here rather than held by the loop: a
    connection kept open across a minute's sleep is a reader the WAL cannot
    checkpoint past, and the loop it watches would pay for the supervisor's
    idleness. The heartbeat goes in after the sweep, stamped with the same
    instant, so a reader of `supervisorHeartbeats` who finds a fresh beat
    knows the sweep it vouches for actually ran.

    Prints what `--sweep` would when there is something to say -- a trip or a
    run one strike from one -- and nothing on a healthy pass: a watcher that
    prints "all healthy" once a minute all night has buried the one line
    that mattered by morning.
    """
    out = out or sys.stdout
    now = int(time() * 1000) if now is None else now
    conn = open_store()
    try:
        seen = sweep(conn, now, act=True, provider=provider)
        if seen.trips or seen.watched:
            print("\n".join(sweep_lines(seen)), file=out)
        store.record_supervisor_heartbeat(conn, pid, started_at, now)
    finally:
        conn.close()
    return seen


def supervise(provider=None, interval=None, wait=None, out=None):
    """`--supervise`'s whole body: lock, sweep, sleep, repeat until a signal.

    The lock is taken before the first pass and given back on every way out
    -- a signal, a pass that raised -- so a supervisor that dies leaves the
    target free for the next one, and the one case the lock stays behind is
    a process killed without the chance, which `acquire_supervisor_lock()`'s
    dead-pid reclaim is for. The signal handlers set a flag the loop reads
    rather than raising into whatever the pass was doing, so a signal that
    lands mid-sweep lets the sweep's transaction finish and the pass that
    was in hand is a whole pass or none. The previous handlers are put back
    afterwards for the same reason `retarget()` is undone in tests: this is
    a mode of a module other code imports.

    `wait` is the sleep, injectable so a test can drive the loop without
    one; it is called with the interval and its result is ignored. The
    default waits on the stop flag itself, so a signal ends the sleep at
    once instead of a minute later. `interval` defaults to the target's
    `[supervisor] sweep_interval_sec`.
    """
    out = out or sys.stdout
    interval = sweep_config().sweep_interval_sec if interval is None else interval
    pid = os.getpid()
    started_at = int(time() * 1000)
    path = acquire_supervisor_lock(supervisor_lock_path(), pid, started_at)
    stop = threading.Event()
    wait = stop.wait if wait is None else wait

    def on_signal(signum, _frame):
        stop.set()

    previous = {signum: signal.signal(signum, on_signal)
                for signum in STOP_SIGNALS}
    try:
        print(f"[holo2] supervising {TARGET} as pid {pid}: acting sweep"
              f" every {interval}s, lock at {path}", file=out)
        while not stop.is_set():
            supervise_pass(pid, started_at, provider=provider, out=out)
            wait(interval)
        print("[holo2] supervisor stopping on signal; lock released",
              file=out)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        release_supervisor_lock(path, pid)
    return 0


def cli(argv=None):
    """Parse the command line and run the mode it names.

    An explicit parser rather than `sys.argv[1]`, which is what the target
    path used to be read from at import: every first argument was a repository
    path, so `--help` named a repository called "--help" and a mistyped flag
    started a real loop somewhere unintended. Both are now argparse errors,
    and the module can be imported without a command line at all.
    """
    parser = argparse.ArgumentParser(
        prog="factory.py",
        description="Holophyte: a minimal Linear-driven software factory.")
    parser.add_argument(
        "target", nargs="?", default=str(DEFAULT_TARGET),
        help="repository the loop works in (default: %(default)s)")
    # The read-only modes, exclusive of each other: each one prints its table
    # and exits, so a command line naming both is a mistake argparse should
    # answer rather than a silent choice between them.
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--report", action="store_true",
        help="print the target store's estimate-vs-actual table and exit; "
             "reads only -- claims no ticket, cuts no worktree, calls nobody")
    modes.add_argument(
        "--sweep", action="store_true",
        help="print the live runs that have tripped a mechanical condition "
             "(dead heartbeat, blown time box, stuck review) and exit; acts "
             "on none of them unless --act says to")
    modes.add_argument(
        "--supervise", action="store_true",
        help="run the acting sweep on an interval ([supervisor] "
             "sweep_interval_sec, default %ds) until SIGINT/SIGTERM, as the "
             "target's one supervisor: a second one for the same target "
             "exits naming the first" % SUPERVISE_INTERVAL_SEC)
    # Not a mode of its own: it says what `--sweep` does with what it finds,
    # so it is refused rather than ignored anywhere else. Silently doing
    # nothing would be the worse answer for the operator who typed
    # `--act` meaning to clean up and got a read-only pass.
    parser.add_argument(
        "--act", action="store_true",
        help="with --sweep: fail each tripped run and release its leases, "
             "leaving its branch and worktree for a human")
    args = parser.parse_args(argv)
    if args.act and not args.sweep:
        parser.error("--act says what --sweep does with the runs it finds; "
                     "it has nothing to act on by itself")
    retarget(args.target)
    # Read the target's config here, with the command line parsed and nothing
    # claimed yet: a malformed file is a startup error about the repository
    # this invocation names, and `--help` never had to touch a config at all.
    config()
    # And the `[supervisor]` table is checked in the same breath, for every
    # mode: the loop's startup self-sweep, `--sweep` and `--supervise` all
    # read it, and a threshold outside its constraint is the same kind of
    # mistake as a file that does not parse -- an error about the config,
    # before anything is claimed, rather than a sweep with numbers nobody
    # chose. Unknown keys in any table the factory reads are refused in the
    # same window: a typo the factory ignored would leave the operator
    # believing a knob is set that is not.
    check_config_keys()
    sweep_config()
    if args.report:
        return report()
    # Same window and the same reasons as `--report`: it reads runs and prints
    # them, so no route has to resolve and nobody is called. `--act` fails
    # runs rather than dispatching them, so it needs no route either.
    if args.sweep:
        return sweep_report(act=args.act)
    # The acting sweep on a timer. Like `--sweep --act` it dispatches nothing
    # and so resolves no route; unlike it, it takes the target's supervisor
    # lock first, and a target that already has one is an exit, not a loop.
    if args.supervise:
        try:
            return supervise()
        except SupervisorHeld as held:
            raise SystemExit(str(held)) from None
    # And, on the path that actually dispatches agents, every route the config
    # names resolves before the loop claims a ticket. `--report` skips this: it
    # calls nobody, so a reviewer that is not installed on the machine reading
    # the table is not that reading's problem.
    check_agent_commands()
    # Same window, same reason: the `[worktree]` table is read here rather
    # than by the first run that cuts a worktree with it.
    check_worktree_setup()
    return main()


if __name__ == "__main__":
    sys.exit(cli())
