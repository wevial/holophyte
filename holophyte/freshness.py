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

The landmarks can all be there and the ticket still overtaken: a merge
since filing did the work, or moved the design on (KO-715). A ticket
`critic_due()` names -- filed more than `[loop] critic_after_hours` ago, or
naming a file `main` changed since -- is put to the `[agents.critic]` seat
once, with `critic_brief()`: the body and what merged since. The answer's
last line (`parse_freshness()`) decides: FRESH claims, STALE or UNSURE
parks through `park_stale()`, under the claim's lease turn so a sibling
loop's claim made meanwhile stands. The critic never blocks the queue on
its own failure: a turn that raises or answers no verdict claims anyway,
and the run the claim opens carries a `warning` naming the failure.

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
import time
from pathlib import Path

import store
import store.read
import ticket_template
from holophyte.agent_routes import routes
from holophyte.board import (
    comment_body,
    lease_turn,
    mirror_key,
    mirror_task,
    note_problems,
    store_status,
    warn,
)
from holophyte.config_tables import loop_config
from holophyte.files import RangeError, touched_files
from holophyte.harness import critic_seat
from holophyte.redact import safe_print as print

STALE_HEADING = "Not claimed: this ticket is out of date with main"
BACKLOG_STATE = "Backlog"
STALE_LABEL = "stale"
HOUR_MS = 3600 * 1000
# The critic's cap, in seconds: a turn is about half a minute.
CRITIC_TIMEOUT = 300
# The merge history is read a page at a time, back to the filing; a brief
# names the first files of each merge.
MERGE_PAGE = 50
BRIEF_FILES = 20
CRITIC_BRIEF = """\
You are the critic. Decide whether the ticket below is still relevant to
the code on `main`, the checkout you are in, or whether work merged since
it was filed already did it or changed the design it assumes. You may read
the code; change nothing.

Ticket {identifier}: {title}
<ticket body>
{body}
</ticket body>

Merged since the ticket was filed (identifier, title: changed files):
{merges}

End your reply with exactly one of these lines, and nothing after it:
FRESHNESS: FRESH
FRESHNESS: STALE <one-line reason>
FRESHNESS: UNSURE <one-line reason>
"""
FRESHNESS_LINE = re.compile(r"FRESHNESS:\s+(FRESH|STALE|UNSURE)(?:\s+(.*))?")
# The claim's warning for a due ticket the critic could not judge, by task
# identifier, until the run the claim opens takes it (`carry_warning()`).
WARNINGS = {}


# A code span naming a function (`name()`, `module.name()`; group 1 is the
# name searched for) or a CapWords class (`Name`, `HTTPServer`): a capital,
# then letters and digits with at least one lowercase, so an all-caps word
# or constant (`HTTP`, `MAX_RUNS`) is not one.
FUNCTION_SPAN_RE = re.compile(r"(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)\(\)")
CLASS_SPAN_RE = re.compile(r"[A-Z][A-Z0-9]*[a-z][A-Za-z0-9]*")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode == 0


def _main_text(repo, path):
    """`path`'s text on `main`, or None when main has no such file -- a
    directory included: `cat-file blob` refuses a tree, where `show` would
    print its listing as if it were the file's text."""
    r = subprocess.run(["git", "-C", str(repo), "cat-file", "blob",
                        f"main:{path}"],
                       capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else None


def _declared_new(path, declarations):
    files, directories = declarations
    normalized = str(Path(path))
    return (normalized in files
            or any(normalized.startswith(d + "/") for d in directories))


def named_paths(body):
    """`(label, path)` for each file `body` names in a code span in its
    acceptance criteria and implementation notes, first mention only,
    skipping the ones the body declares new."""
    if body is None:
        return []
    t = ticket_template.parse(body)
    declarations = ticket_template._new_paths(t)
    texts = [(f"Acceptance criteria #{i}", text)
             for i, text in enumerate(t.acceptance_boxes, 1)]
    texts.append(("Implementation notes",
                  t.sections.get("Implementation notes", "")))
    named, seen = [], set()
    for label, text in texts:
        for _, path in ticket_template._prose_paths(text):
            normalized = str(Path(path))
            if normalized in seen or _declared_new(path, declarations):
                continue
            seen.add(normalized)
            named.append((label, path))
    return named


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
        reasons += [f"`{path}` (named in {label}) is not on main"
                    for label, path in named_paths(body)
                    if not _git(repo, "cat-file", "-e", f"main:{Path(path)}")]
        reasons += _missing_symbols(repo, t, ticket_template._new_paths(t))
    return reasons + _unmerged_dependencies(t, conn, provider)


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
    notes = t.sections.get("Implementation notes", "")
    for i, item in enumerate(ticket_template._list_item_blocks(notes), 1):
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
    mirror says `merged`, or else the board answers `completed` -- over an
    `abandoned` mirror too. A board that cannot be asked gives no evidence,
    so every dependency the store does not hold merged is refused."""
    status = {}
    for dep in t.depends_on or []:
        mirror = (store.read.ticket_by_identifier(conn, dep)
                  if conn is not None else None)
        status[dep] = mirror.status if mirror is not None else None
    unmerged = [d for d, s in status.items() if s != "merged"]
    closed, unasked = {}, ""
    if unmerged and provider is not None:
        try:
            closed = provider.closed_identifiers(unmerged)
        except Exception as e:  # any transport failure: the board was not asked
            unasked = f" (the board could not be asked: {e})"
    reasons = []
    for dep in unmerged:
        answer = closed.get(dep)
        if answer == "completed":
            continue
        canceled = answer == "canceled" or (answer is None
                                            and status[dep] == "abandoned")
        verdict = "canceled" if canceled else "not merged"
        reasons.append(f"`{dep}` (named in Depends on) is {verdict}{unasked}")
    return reasons


def stale_comment(reasons):
    """The one board comment a stale ticket gets."""
    lines = "\n".join(f"* {reason}" for reason in reasons)
    return (f"**{STALE_HEADING}**\n\n{lines}\n\nUpdate the body to name"
            " what main holds now, or wait for its dependencies to merge,"
            " then move the issue back to Todo.")


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


def park_stale(project, conn, project_id, provider, task, reasons, why=None,
               admitted=False):
    """Refuse a stale ticket: mirror it `needs_spec`, comment once, label
    the issue `stale`, move it to Backlog, print the skip line, which `why`
    words when the reasons are not missing files. A board call that fails
    is a warning; the ticket is skipped either way and the loop goes on.

    Serialized with the claim (KO-715): the row is re-read and the mirror
    written under the claim's `lease_turn()`, so no sibling loop's
    `store.claim()` lands in between. A ticket a live run holds by then is
    that run's -- its contract is not blanked, its issue not moved -- and so
    is an `admitted` one, `ready` when this pass admitted it, that is no
    longer `ready`: another loop parked or moved it while the critic ran.
    Either is skipped with nothing written.

    A store-mode board (KO-745) is not commented on or moved: the comment is
    a `stale` note keyed on the body and the reasons, and the move a queued
    push, so the next observation reads Backlog as the factory's move and
    not a person's. The label is still written inline. A native board
    (KO-759) is the store: the note is written and nothing else, no label
    and no queued push.

    A task a store-mode claim built at a revision the ticket has since
    left (Phase 3 stage 3) was judged on a body the board no longer holds:
    that verdict is dropped the same way, with nothing written."""
    issue_id = mirror_key(task)
    with lease_turn(project), store.transaction(conn):
        row = conn.execute(
            "SELECT activeRunId, status, revision FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (issue_id, project_id)).fetchone()
        taken = row is not None and (
            row[0] is not None or (admitted and row[1] != "ready"))
        moved = row is not None and task.get("store_revision") not in (
            None, row[2])
        if not (taken or moved):
            ticket_id = mirror_task(conn, project_id, task, specced=False)
    if taken or moved:
        why = ("another loop claimed or parked it" if taken
               else "it changed on the board")
        print(f"[holo2] {task['id']} skipped: {why} while it was judged;"
              " this verdict is dropped")
        return
    store_mode = getattr(provider, "store_mode", False) is True
    if store_mode:
        note_problems(conn, ticket_id, "stale", task.get("body"), reasons,
                      stale_comment(reasons))
    else:
        try:
            provider.comment(issue_id, comment_body(stale_comment(reasons)))
        except Exception as e:
            warn(conn, ticket_id, f"stale-ticket comment failed for"
                                  f" {task['id']} ({e}); the board is not"
                                  " told why")
    if getattr(provider, "native", False) is not True:
        _label_and_move(conn, provider, task, issue_id, ticket_id, store_mode)
    why = why or f"{len(reasons)} stale landmarks"
    print(f"[holo2] {task['id']} skipped: out of date with main ({why})")


def _label_and_move(conn, provider, task, issue_id, ticket_id, store_mode):
    """Label a parked ticket's issue `stale` and move it to Backlog, the
    move queued on a store-mode board; a board call that fails is a
    warning."""
    try:
        provider.label_issue(issue_id, STALE_LABEL)
    except Exception as e:
        warn(conn, ticket_id, f"stale label failed for {task['id']} ({e});"
                              " the board carries no mark")
    if store_mode:
        store.record_push(conn, ticket_id, BACKLOG_STATE)
    else:
        try:
            provider.set_state(issue_id, BACKLOG_STATE)
        except Exception as e:
            warn(conn, ticket_id, f"moving stale {task['id']} to"
                                  f" {BACKLOG_STATE} failed ({e}); the board"
                                  " still lists it ready")


def critic_due(project, task):
    """Whether the claim asks the critic about `task`: the target has a
    critic seat its startup probe did not turn off, and the ticket was
    filed more than `[loop] critic_after_hours` ago, or `main` has a
    commit since its filing that touches a file its body names. A task
    with no `filed_at` is never due."""
    filed = task.get("filed_at")
    if (filed is None or critic_seat(project) is None
            or routes(project).critic_failed):
        return False
    hours = loop_config(project).critic_after_hours
    if time.time() * 1000 - filed > hours * HOUR_MS:
        return True
    paths = [str(Path(path)) for _, path in named_paths(task.get("body"))]
    if not paths:
        return False
    touched = subprocess.run(
        ["git", "-C", str(project.path), "log", "main",
         f"--since=@{filed // 1000}", "--format=%H", "--", *paths],
        capture_output=True, text=True)
    return touched.returncode == 0 and bool(touched.stdout.strip())


def _changed_files(project, run):
    """The files `run`'s merge changed, as one brief line's tail."""
    if not run.mergeSha:
        return "(no merge commit recorded)"
    try:
        touched = touched_files(project.path, None, run.mergeSha,
                                cap=BRIEF_FILES)
    except (RangeError, RuntimeError, subprocess.TimeoutExpired) as e:
        return f"(changed files unknown: {str(e).splitlines()[0]})"
    names = ", ".join(f.path for f in touched.files) or "(none)"
    return names + (", ..." if touched.truncated else "")


def merged_since(conn, filed):
    """Every merged run that ended after `filed`, newest first, paged back
    through `merged_runs()` until a page reaches the filing."""
    before = None
    while True:
        page = store.read.merged_runs(conn, MERGE_PAGE, before)
        for run in page:
            if run.endedAt <= filed:
                return
            yield run
        if len(page) < MERGE_PAGE:
            return
        before = page[-1].id


def critic_brief(conn, project, task):
    """The critic's goal: the ticket body, one line per run merged since
    `filed_at` -- identifier, title and the files its merge changed --
    newest first, then the answer contract."""
    filed = task.get("filed_at") or 0
    merges = [f"- {run.linearIdentifier} {run.title}: "
              f"{_changed_files(project, run)}"
              for run in merged_since(conn, filed)]
    return CRITIC_BRIEF.format(
        identifier=task["id"], title=task.get("title", ""),
        body=(task.get("body") or "").strip(),
        merges="\n".join(merges) or "- none")


def parse_freshness(output):
    """The critic's verdict from its last non-empty line: `("fresh", "")`,
    `("stale", reason)` or `("unsure", reason)`; None for anything else:
    a FRESH with text after it, or a STALE or UNSURE with no reason."""
    lines = [line.strip() for line in (output or "").splitlines()
             if line.strip()]
    match = FRESHNESS_LINE.fullmatch(lines[-1].strip("*` ")) if lines else None
    if match is None:
        return None
    verdict, reason = match.group(1).lower(), (match.group(2) or "").strip()
    if verdict == "fresh":
        return None if reason else (verdict, "")
    return (verdict, reason) if reason else None


def ask_critic(conn, project, task):
    """One critic turn on `task` in a `critic_workspace()`; its output.
    Through `holophyte.loop.agent`, read at call time, so a test's patch
    of the loop's agent answers it."""
    import holophyte.loop
    from holophyte.agents import critic_workspace
    goal = critic_brief(conn, project, task)
    with critic_workspace(project) as checkout:
        return str(holophyte.loop.agent(project, "critic", goal, checkout,
                                        timeout=CRITIC_TIMEOUT))


def _failure(error):
    """A failed critic turn in one line, never the argv (the brief)."""
    if isinstance(error, subprocess.TimeoutExpired):
        return f"timed out after {error.timeout:g} seconds"
    first = (str(error).splitlines() or [""])[0][:200]
    return f"{type(error).__name__}: {first}" if first else type(error).__name__


def critic_admits(project, conn, project_id, provider, task):
    """The claim's critic question: False when the critic parked `task`.

    A ticket not `critic_due()` is admitted unasked. FRESH admits; STALE
    and UNSURE park it through `park_stale()`, the reason prefixed
    `critic: stale` or `critic: unsure`. A turn that raises or times out,
    or an answer `parse_freshness()` reads no verdict in, admits it too,
    with a warning `carry_warning()` records on the run the claim opens.
    """
    WARNINGS.pop(task["id"], None)
    if not critic_due(project, task):
        return True
    try:
        answer = parse_freshness(ask_critic(conn, project, task))
        failure = None if answer else "its answer ended in no FRESHNESS verdict"
    except Exception as e:
        answer, failure = None, f"its turn failed ({_failure(e)})"
    if failure:
        WARNINGS[task["id"]] = (f"critic: {failure}; {task['id']} claimed"
                                " without the relevance check")
        print(f"[holo2] {WARNINGS[task['id']]}")
        return True
    verdict, reason = answer
    if verdict == "fresh":
        return True
    park_stale(project, conn, project_id, provider, task,
               [f"critic: {verdict} \u2014 {reason}"],
               why=f"the critic answered {verdict.upper()}", admitted=True)
    return False


def carry_warning(conn, run_id, task):
    """Record the claim's critic warning for `task`, if any, as a `warning`
    event on `run_id`, the run the claim opened."""
    warning = WARNINGS.pop(task["id"], None)
    if warning is not None:
        store.record_event(conn, run_id, "warning", warning)


def parked_since_admitted(conn, ticket_id, task):
    """True, with the skip line printed, when a sibling critic parked
    `ticket_id` since this pass admitted it (KO-715): read under the claim's
    `lease_turn()`, which `park_stale()` also takes, so the ticket is
    skipped rather than claimed and refused."""
    if store_status(conn, ticket_id) != "needs_spec":
        return False
    print(f"[holo2] {task['id']} was parked since it was admitted; skipping it")
    return True
