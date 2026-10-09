import json
import re
import subprocess
import time
from pathlib import Path

import store
import store.board
import store.read
import ticket_template
from holophyte.agents.agent_routes import routes
from holophyte.agents.harness import critic_seat
from holophyte.board.projection import (
    comment_body,
    lease_turn,
    mirror_key,
    mirror_task,
    note_problems,
    store_status,
    warn,
)
from holophyte.config.config_tables import loop_config
from holophyte.files import RangeError, touched_files
from holophyte.redact import safe_print as print

STALE_HEADING = "Not claimed: this ticket is out of date with main"
BACKLOG_STATE = "Backlog"
STALE_LABEL = "stale"
HOUR_MS = 3600 * 1000
CRITIC_TIMEOUT = 300
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
WARNINGS = {}


FUNCTION_SPAN_RE = re.compile(r"(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)\(\)")
# At least one lowercase letter, so a constant like `MAX_RUNS` is no class.
CLASS_SPAN_RE = re.compile(r"[A-Z][A-Z0-9]*[a-z][A-Za-z0-9]*")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode == 0


def _main_text(repo, path):
    # `cat-file blob` refuses a directory, which `show` would list as text.
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
    if getattr(provider, "native", False) is True:
        return []
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
        except Exception as e:
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


def stale_comment(reasons, identifier=None):
    lines = "\n".join(f"* {reason}" for reason in reasons)
    head = (f"**{STALE_HEADING}**\n\n{lines}\n\nUpdate the body to name"
            " what main holds now, or wait for its dependencies to merge,")
    if identifier is None:
        return head + " then move the issue back to Todo."
    return (head + f" then `--file-ticket --update {identifier}` re-readies"
            f" it; `--move {identifier} ready` re-checks it against main as"
            " it stands.")


def skip_labelled_stale(conn, project_id, task):
    if STALE_LABEL not in (task.get("labels") or []):
        return False
    mirror_task(conn, project_id, task, specced=False)
    print(f"[holo2] {task['id']} skipped: labelled {STALE_LABEL};"
          " fix the body and remove the label")
    return True


def park_stale(project, conn, project_id, provider, task, reasons, why=None,
               admitted=False, kind="stale"):
    issue_id = mirror_key(task)
    with lease_turn(project), store.transaction(conn):
        row = conn.execute(
            "SELECT activeRunId, status, revision, id FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (issue_id, project_id)).fetchone()
        taken = row is not None and (
            row[0] is not None or (admitted and row[1] != "ready"))
        moved = row is not None and task.get("store_revision") not in (
            None, row[2])
        store_mode = getattr(provider, "store_mode", False) is True
        native = getattr(provider, "native", False) is True
        if not (taken or moved):
            salt = None if row is None else _other_park_note(conn, row[3],
                                                             kind)
            ticket_id = mirror_task(conn, project_id, task, specced=False)
            if store_mode:
                note_problems(conn, ticket_id, kind, task.get("body"), reasons,
                              stale_comment(reasons,
                                            task["id"] if native else None),
                              salt=salt)
    if taken or moved:
        why = ("another loop claimed or parked it" if taken
               else "it changed on the board")
        print(f"[holo2] {task['id']} skipped: {why} while it was judged;"
              " this verdict is dropped")
        return
    if not store_mode:
        try:
            provider.comment(issue_id, comment_body(stale_comment(reasons)))
        except Exception as e:
            warn(conn, ticket_id, f"stale-ticket comment failed for"
                                  f" {task['id']} ({e}); the board is not"
                                  " told why")
    if not native:
        _label_and_move(conn, provider, task, issue_id, ticket_id, store_mode)
    why = why or f"{len(reasons)} stale landmarks"
    print(f"[holo2] {task['id']} skipped: out of date with main ({why})")


def _label_and_move(conn, provider, task, issue_id, ticket_id, store_mode):
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


_NEWEST_PARK_NOTE = (
    "SELECT id FROM ticketNotes WHERE ticketId = {}"
    " AND kind IN ('stale', 'critic', 'validation')"
    " ORDER BY at DESC, id DESC LIMIT 1")


def _other_park_note(conn, ticket_id, kind):
    row = conn.execute(
        "SELECT id FROM ticketNotes WHERE ticketId = ?"
        " AND kind IN ('stale', 'critic', 'validation')"
        " AND CASE WHEN kind = 'stale' AND text LIKE '%* critic: %'"
        " THEN 'critic' ELSE kind END != ?"
        " ORDER BY at DESC, id DESC LIMIT 1", (ticket_id, kind)).fetchone()
    return None if row is None else row[0]


def stale_parked(conn, project_id, identifier=None, columns=("ready",),
                 critic=False):
    return conn.execute(
        "SELECT t.id, t.linearIdentifier, t.revision, t.body, t.boardColumn,"
        " n.id, CASE WHEN n.kind = 'critic' OR n.text LIKE '%* critic: %'"
        " THEN 'critic' ELSE 'stale' END"
        " FROM tickets t JOIN ticketNotes n ON n.id = ("
        + _NEWEST_PARK_NOTE.format("t.id") + ")"
        " WHERE t.projectId = ? AND t.status = 'needs_spec'"
        " AND t.activeRunId IS NULL AND t.goneSince IS NULL"
        " AND t.boardColumn IN (SELECT value FROM json_each(?))"
        " AND (n.kind = 'stale' AND n.text NOT LIKE '%* critic: %'"
        " OR ? AND n.kind = 'critic' OR ? AND n.kind = 'stale')"
        " AND (? IS NULL OR t.linearIdentifier = ?)"
        " ORDER BY t.id",
        (project_id, json.dumps(list(columns)), critic, critic, identifier,
         identifier)).fetchall()


def _refreshed(target, conn):
    from holophyte.loop.claim import refresh_main
    from holophyte.loop.gates import InfraFailure
    try:
        refresh_main(target, conn=conn, before="the stale re-check")
    except (InfraFailure, RuntimeError) as e:
        return " ".join(str(e).split())
    return None


def _still_parked(conn, project_id, parked, columns, critic=False):
    identifier, note_id = parked[1], parked[5]
    if [row[5] for row in stale_parked(conn, project_id, identifier, columns,
                                       critic)] != [note_id]:
        raise store.board.FilingRefused([
            f"{identifier} was claimed, edited or parked again while it was"
            " re-checked; nothing changed"])


def _rederive(target, conn, project_id, parked, revision, note=None):
    ticket_id, identifier, _, body = parked[:4]
    revision = store.board.edit_ticket(conn, project_id, identifier, body,
                                       revision, author="factory")
    status = store_status(conn, ticket_id)
    if status != "needs_spec":
        head = subprocess.run(
            ["git", "-C", str(target.path), "rev-parse", "--short=12", "main"],
            capture_output=True, text=True).stdout.strip()
        now = int(time.time() * 1000)
        store.record_note(
            conn, ticket_id, "recheck",
            f"Re-checked against main at {head}: every landmark the stale park"
            f" named is there now, so {identifier} went from needs_spec to"
            f" {status} (revision {revision})."
            + (f"\n\n{note}" if note else ""),
            f"recheck:{ticket_id}:{now}", now=now)
    return revision, status


def recheck_stale(target, conn, project_id, provider):
    parked = stale_parked(conn, project_id)
    if not parked:
        return
    failure = _refreshed(target, conn)
    if failure is not None:
        print(f"[holo2] main not refreshed, so no stale-parked ticket"
              f" ({len(parked)}) is re-checked this pass: {failure}")
        return
    for row in parked:
        if stale_reasons(target.path, row[3], conn, provider):
            continue
        try:
            with lease_turn(target), store.transaction(conn):
                _still_parked(conn, project_id, row, ("ready",))
                revision, status = _rederive(target, conn, project_id, row,
                                             row[2])
        except (ValueError, store.RevisionMoved) as e:
            print(f"[holo2] {row[1]} not re-checked: {e}")
            continue
        print(f"[holo2] re-checked {row[1]} against main: {status}"
              f" (revision {revision})")


def recheck_move(target, conn, project_id, provider, parked, revision, note):
    _, identifier, current, body, column, _, kind = parked
    if current != revision:
        raise store.RevisionMoved(identifier, revision, current)
    failure = _refreshed(target, conn)
    if failure is not None:
        raise store.board.FilingRefused([f"main not refreshed, so {identifier}"
                                         f" is not re-checked: {failure}"])
    reasons = stale_reasons(target.path, body, conn, provider)
    if reasons:
        raise store.board.FilingRefused([f"{identifier} stays parked: "
                                         + "; ".join(reasons)])
    moved = column != "ready"
    with lease_turn(target), store.transaction(conn):
        _still_parked(conn, project_id, parked, ("ready", "backlog"),
                      critic=True)
        if moved:
            revision = store.board.move_ticket(conn, project_id, identifier,
                                               "ready", revision,
                                               author="cli", note=note)
        revision, status = _rederive(target, conn, project_id, parked,
                                     revision, None if moved else note)
        if status != "needs_spec":
            store.record_project_intervention(
                conn, "requeue", f"--move {identifier} ready re-readied it"
                f" from a {kind} park: {status} at revision {revision}",
                source="human", trigger="manual", project_id=project_id)
    return revision, moved, status


def critic_due(project, task):
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
    filed = task.get("filed_at") or 0
    merges = [f"- {run.linearIdentifier} {run.title}: "
              f"{_changed_files(project, run)}"
              for run in merged_since(conn, filed)]
    return CRITIC_BRIEF.format(
        identifier=task["id"], title=task.get("title", ""),
        body=(task.get("body") or "").strip(),
        merges="\n".join(merges) or "- none")


def parse_freshness(output):
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
    import holophyte.loop.review_round
    from holophyte.agents.review_workspace import critic_workspace
    goal = critic_brief(conn, project, task)
    with critic_workspace(project) as checkout:
        return str(holophyte.loop.review_round.agent(project, "critic", goal, checkout,
                                        timeout=CRITIC_TIMEOUT))


def _failure(error):
    if isinstance(error, subprocess.TimeoutExpired):
        return f"timed out after {error.timeout:g} seconds"
    first = (str(error).splitlines() or [""])[0][:200]
    return f"{type(error).__name__}: {first}" if first else type(error).__name__


def critic_admits(project, conn, project_id, provider, task):
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
               why=f"the critic answered {verdict.upper()}", admitted=True,
               kind="critic")
    return False


def carry_warning(conn, run_id, task):
    warning = WARNINGS.pop(task["id"], None)
    if warning is not None:
        store.record_event(conn, run_id, "warning", warning)


def parked_since_admitted(conn, ticket_id, task):
    if store_status(conn, ticket_id) != "needs_spec":
        return False
    print(f"[holo2] {task['id']} was parked since it was admitted; skipping it")
    return True
