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

The landmarks can all be there and the ticket still overtaken: a merge
since filing did the work, or moved the design on (KO-715). A ticket
`critic_due()` names -- filed more than `[loop] critic_after_hours` ago, or
naming a file `main` changed since -- is put to the `[agents.critic]` seat
once, with `critic_brief()`: the body and what merged since. The answer's
last line (`parse_freshness()`) decides: FRESH claims, STALE or UNSURE
parks through `park_stale()`. The critic never blocks the queue on its own
failure: a turn that raises or answers no verdict claims anyway, and the
run the claim opens carries a `warning` naming the failure.
"""
import re
import subprocess
import time
from pathlib import Path

import store
import store.read
import ticket_template
from holophyte.agent_routes import routes
from holophyte.board import comment_body, mirror_key, mirror_task, warn
from holophyte.config_tables import loop_config
from holophyte.files import RangeError, touched_files
from holophyte.harness import critic_seat
from holophyte.redact import safe_print as print

STALE_HEADING = "Not claimed: this ticket is out of date with main"
BACKLOG_STATE = "Backlog"
HOUR_MS = 3600 * 1000
# The critic's cap, in seconds: a turn is about half a minute.
CRITIC_TIMEOUT = 300
# How much of the merge history a brief carries: the newest merges, and the
# first files of each.
BRIEF_MERGES = 50
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


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode == 0


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
    return [f"`{path}` (named in {label}) is not on main"
            for label, path in named_paths(body)
            if not _git(repo, "cat-file", "-e", f"main:{Path(path)}")]


def stale_comment(reasons):
    """The one board comment a stale ticket gets."""
    lines = "\n".join(f"* {reason}" for reason in reasons)
    return (f"**{STALE_HEADING}**\n\n{lines}\n\nUpdate the body to name"
            " what main holds now, then move the issue back to Todo.")


def park_stale(conn, project_id, provider, task, reasons, why=None):
    """Refuse a stale ticket: mirror it `needs_spec`, comment once, move the
    issue to Backlog, print the skip line, which `why` words when the
    reasons are not missing files. A board call that fails is a warning;
    the ticket is skipped either way and the loop goes on."""
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
    why = why or f"{len(reasons)} named files missing"
    print(f"[holo2] {task['id']} skipped: out of date with main ({why})")


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


def critic_brief(conn, project, task):
    """The critic's goal: the ticket body, one line per run merged since
    `filed_at` -- identifier, title and the files its merge changed --
    newest first, then the answer contract."""
    filed = task.get("filed_at") or 0
    merges = [f"- {run.linearIdentifier} {run.title}: "
              f"{_changed_files(project, run)}"
              for run in store.read.merged_runs(conn, BRIEF_MERGES)
              if run.endedAt > filed]
    return CRITIC_BRIEF.format(
        identifier=task["id"], title=task.get("title", ""),
        body=(task.get("body") or "").strip(),
        merges="\n".join(merges) or "- none")


def parse_freshness(output):
    """The critic's verdict from its last non-empty line: `("fresh", "")`,
    `("stale", reason)` or `("unsure", reason)`; None for anything else,
    a STALE or UNSURE with no reason included."""
    lines = [line.strip() for line in (output or "").splitlines()
             if line.strip()]
    match = FRESHNESS_LINE.fullmatch(lines[-1].strip("*` ")) if lines else None
    if match is None:
        return None
    verdict, reason = match.group(1).lower(), (match.group(2) or "").strip()
    if verdict == "fresh":
        return verdict, ""
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
    park_stale(conn, project_id, provider, task,
               [f"critic: {verdict} \u2014 {reason}"],
               why=f"the critic answered {verdict.upper()}")
    return False


def carry_warning(conn, run_id, task):
    """Record the claim's critic warning for `task`, if any, as a `warning`
    event on `run_id`, the run the claim opened."""
    warning = WARNINGS.pop(task["id"], None)
    if warning is not None:
        store.record_event(conn, run_id, "warning", warning)
