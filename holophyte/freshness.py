"""The claim's freshness check (KO-709): a ticket against main as it is now.

A ticket is judged against the repository when it is filed, and since
KO-381 a named path that does not exist is only an advisory there, so a
ticket filed against files a later merge moved or deleted used to be
claimed as if nothing had changed. `stale_reasons()` asks `main` -- the
ref, through git, not the checkout's working tree, which may be on
another branch -- for every file the body names in a code span in its
acceptance criteria and implementation notes, skipping the ones the body
declares new. `park_stale()` is the refusal: the mirror lands in
`needs_spec`, the board issue gets one comment, a `stale` label and a
move to Backlog, which the ready listing does not read, so the ticket is
not offered again until its maintainer moves it back. The label is the
visible mark (KO-716): the claim skips an issue carrying it, so a ticket
dragged back to Todo unfixed is not checked again, and the maintainer
takes it off once the body is fixed.
"""
import subprocess
from pathlib import Path

import ticket_template
from holophyte.board import comment_body, mirror_key, mirror_task, warn
from holophyte.redact import safe_print as print

STALE_HEADING = "Not claimed: this ticket is out of date with main"
BACKLOG_STATE = "Backlog"
STALE_LABEL = "stale"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode == 0


def _declared_new(path, declarations):
    files, directories = declarations
    normalized = str(Path(path))
    return (normalized in files
            or any(normalized.startswith(d + "/") for d in directories))


def stale_reasons(repo, body):
    """One reason per named file absent from `main`, in body order.

    Paths are found as the validator finds them (`_prose_paths()` over the
    criteria and the implementation notes; `_new_paths()` for the new
    ones). A repository with no `main` commit has nothing to judge against
    and yields none, so the check never refuses a ticket on git's say-so
    about the ref rather than the path.
    """
    if body is None or not _git(repo, "rev-parse", "--verify", "-q",
                                "main^{commit}"):
        return []
    t = ticket_template.parse(body)
    declarations = ticket_template._new_paths(t)
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


def stale_comment(reasons):
    """The one board comment a stale ticket gets."""
    lines = "\n".join(f"* {reason}" for reason in reasons)
    return (f"**{STALE_HEADING}**\n\n{lines}\n\nUpdate the body to name"
            " what main holds now, then move the issue back to Todo.")


def skip_labelled_stale(conn, project_id, task):
    """Skip an issue carrying the `stale` label (KO-716), the maintainer's
    mark that the body is still out of date: mirror it `needs_spec` and
    print why, without asking main again or commenting a second time.
    Returns whether the issue was skipped."""
    if STALE_LABEL not in (task.get("labels") or []):
        return False
    mirror_task(conn, project_id, task, specced=False)
    print(f"[holo2] {task['id']} skipped: labelled {STALE_LABEL};"
          " fix the body and remove the label")
    return True


def park_stale(conn, project_id, provider, task, reasons):
    """Refuse a stale ticket: mirror it `needs_spec`, comment once, label
    the issue `stale`, move it to Backlog, print the skip line. A board
    call that fails is a warning; the ticket is skipped either way and the
    loop goes on."""
    ticket_id = mirror_task(conn, project_id, task, specced=False)
    issue_id = mirror_key(task)
    try:
        provider.comment(issue_id, comment_body(stale_comment(reasons)))
    except Exception as e:
        warn(conn, ticket_id, f"stale-ticket comment failed for {task['id']}"
                              f" ({e}); the board is not told why")
    try:
        provider.label_issue(issue_id, STALE_LABEL)
    except Exception as e:
        warn(conn, ticket_id, f"stale label failed for {task['id']} ({e});"
                              " the board carries no mark")
    try:
        provider.set_state(issue_id, BACKLOG_STATE)
    except Exception as e:
        warn(conn, ticket_id, f"moving stale {task['id']} to"
                              f" {BACKLOG_STATE} failed ({e}); the board"
                              " still lists it ready")
    print(f"[holo2] {task['id']} skipped: out of date with main"
          f" ({len(reasons)} named files missing)")
