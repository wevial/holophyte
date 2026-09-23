"""The claim's freshness check (KO-709): a ticket against main as it is now.

A ticket is judged against the repository when it is filed, and since
KO-381 a named path that does not exist is only an advisory there, so a
ticket filed against files a later merge moved or deleted used to be
claimed as if nothing had changed. `stale_reasons()` asks `main` -- the
ref, through git, not the checkout's working tree, which may be on
another branch -- for every file the body names in a code span in its
acceptance criteria and implementation notes, skipping the ones the body
declares new. `park_stale()` is the refusal: the mirror lands in
`needs_spec`, the board issue gets one comment and moves to Backlog,
which the ready listing does not read, so the ticket is not offered
again until its maintainer moves it back.

KO-713 adds two more stale landmarks. A function or class an
implementation-notes item names beside a file must still occur, as a
whole word, in one of that item's files on `main` -- lenient on purpose:
a name used there but defined elsewhere passes, a renamed or removed one
does not. And every `Depends on:` ticket must be merged, by the store's
mirror or the board's word, whether or not the board relation was ever
recorded.
"""
import re
import subprocess
from pathlib import Path

import store.read
import ticket_template
from holophyte.board import comment_body, mirror_key, mirror_task, warn
from holophyte.redact import safe_print as print

STALE_HEADING = "Not claimed: this ticket is out of date with main"
BACKLOG_STATE = "Backlog"


# A code span naming a function (`name()`, `module.name()`; group 1 is the
# name searched for) or a CapWords class (`Name`).
FUNCTION_SPAN_RE = re.compile(r"(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)\(\)")
CLASS_SPAN_RE = re.compile(r"[A-Z][a-z][A-Za-z0-9]*")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode == 0


def _main_text(repo, path):
    """`path`'s text on `main`, or None when main has no such file."""
    r = subprocess.run(["git", "-C", str(repo), "show", f"main:{path}"],
                       capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else None


def _declared_new(path, declarations):
    files, directories = declarations
    normalized = str(Path(path))
    return (normalized in files
            or any(normalized.startswith(d + "/") for d in directories))


def stale_reasons(repo, body, conn=None, provider=None):
    """One reason per stale landmark in the body, in body order: a named
    file absent from `main`, a named symbol absent from its item's files
    there, then a `Depends on:` ticket not merged.

    Paths are found as the validator finds them (`_prose_paths()` over the
    criteria and the implementation notes; `_new_paths()` for the new
    ones). A repository with no `main` commit has nothing to judge files
    against and yields no file or symbol reason, so the check never
    refuses a ticket on git's say-so about the ref rather than the path.
    The dependency check asks the store `conn` and the board `provider`,
    whichever are given.
    """
    if body is None:
        return []
    t = ticket_template.parse(body)
    reasons = []
    if _git(repo, "rev-parse", "--verify", "-q", "main^{commit}"):
        declarations = ticket_template._new_paths(t)
        reasons += _missing_files(repo, t, declarations)
        reasons += _missing_symbols(repo, t, declarations)
    return reasons + _unmerged_dependencies(t, conn, provider)


def _missing_files(repo, t, declarations):
    texts = [(f"Acceptance criteria #{i}", text)
             for i, text in enumerate(t.acceptance_boxes, 1)]
    texts.append(("Implementation notes",
                  t.sections.get("Implementation notes", "")))
    reasons, seen = [], set()
    for label, text in texts:
        for _, path in ticket_template._prose_paths(text):
            normalized = str(Path(path))
            if normalized in seen or _declared_new(path, declarations):
                continue
            seen.add(normalized)
            if not _git(repo, "cat-file", "-e", f"main:{normalized}"):
                reasons.append(f"`{path}` (named in {label}) is not on main")
    return reasons


def _named_symbols(item):
    """(span, name) per function or class span in `item`, skipping one the
    item declares new: the word "new" before it in the same sentence."""
    masked = ticket_template._mask_code_spans(item)
    for span in re.finditer(r"`([^`\n]+)`", item):
        text = span.group(1)
        function = FUNCTION_SPAN_RE.fullmatch(text)
        if function:
            name = function.group(1)
        elif CLASS_SPAN_RE.fullmatch(text):
            name = text
        else:
            continue
        sentence = ticket_template._sentence_before(masked, span.start())
        if not re.search(r"\bnew\b", sentence, re.I):
            yield text, name


def _missing_symbols(repo, t, declarations):
    """One reason per function or class an implementation-notes item names
    that occurs as a whole word in none of the item's files on `main`. An
    item naming no file main holds (none, only new ones, or only missing
    ones, which `_missing_files()` reports) is not checked."""
    reasons, texts = [], {}
    for i, item in enumerate(t.notes, 1):
        paths = []
        for _, path in ticket_template._prose_paths(item):
            normalized = str(Path(path))
            if normalized in paths or _declared_new(path, declarations):
                continue
            if normalized not in texts:
                texts[normalized] = _main_text(repo, normalized)
            if texts[normalized] is not None:
                paths.append(normalized)
        if not paths:
            continue
        for span, name in _named_symbols(item):
            word = re.compile(rf"\b{re.escape(name)}\b")
            if not any(word.search(texts[p]) for p in paths):
                files = ", ".join(f"`{p}`" for p in paths)
                reasons.append(f"`{span}` (named in Implementation notes #{i})"
                               f" is not in {files} on main")
    return reasons


def _unmerged_dependencies(t, conn, provider):
    """One reason per `Depends on:` ticket that is not merged: the store's
    mirror says `merged`, or else the board answers `completed`. A board
    that cannot be asked refuses nothing on its silence."""
    status = {}
    for dep in t.depends_on or []:
        mirror = (store.read.ticket_by_identifier(conn, dep)
                  if conn is not None else None)
        status[dep] = mirror.status if mirror is not None else None
    open_deps = [d for d, s in status.items() if s not in ("merged", "abandoned")]
    closed = {}
    if open_deps and provider is not None:
        try:
            closed = provider.closed_identifiers(open_deps)
        except Exception as e:  # any transport failure: the board was not asked
            print(f"[holo2] could not ask the board whether {', '.join(open_deps)}"
                  f" merged ({e}); the dependency check is skipped")
            return []
    reasons = []
    for dep, state in status.items():
        answer = closed.get(dep)
        if state == "merged" or answer == "completed":
            continue
        verdict = ("canceled" if state == "abandoned" or answer == "canceled"
                   else "not merged")
        reasons.append(f"`{dep}` (named in Depends on) is {verdict}")
    return reasons


def stale_comment(reasons):
    """The one board comment a stale ticket gets."""
    lines = "\n".join(f"* {reason}" for reason in reasons)
    return (f"**{STALE_HEADING}**\n\n{lines}\n\nUpdate the body to name"
            " what main holds now, or wait for its dependencies to merge,"
            " then move the issue back to Todo.")


def park_stale(conn, project_id, provider, task, reasons):
    """Refuse a stale ticket: mirror it `needs_spec`, comment once, move the
    issue to Backlog, print the skip line. A board call that fails is a
    warning; the ticket is skipped either way and the loop goes on."""
    ticket_id = mirror_task(conn, project_id, task, specced=False)
    issue_id = mirror_key(task)
    try:
        provider.comment(issue_id, comment_body(stale_comment(reasons)))
    except Exception as e:
        warn(conn, ticket_id, f"stale-ticket comment failed for {task['id']}"
                              f" ({e}); the board is not told why")
    try:
        provider.set_state(issue_id, BACKLOG_STATE)
    except Exception as e:
        warn(conn, ticket_id, f"moving stale {task['id']} to"
                              f" {BACKLOG_STATE} failed ({e}); the board"
                              " still lists it ready")
    print(f"[holo2] {task['id']} skipped: out of date with main"
          f" ({len(reasons)} stale landmarks)")
