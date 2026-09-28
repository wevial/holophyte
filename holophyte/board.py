"""The store's ticket state and run narrative, projected onto the board."""
import contextlib
import fcntl
import hashlib
import os
import re
import socket
import sys
from pathlib import Path

import store
import store.board
import store.read
import store.tickets
import ticket_template
from holophyte import deadline
from holophyte.agents import cleanup_review_refs
from holophyte.findings import refresh_findings
from holophyte.redact import outbound, redact_values
from holophyte.redact import safe_print as print
from holophyte.report import host_label
from holophyte.runs import warn_on_run

LEASE_LABEL_PREFIX = "holo:"


def board_owned_labels(labels):
    # "stale" is freshness.STALE_LABEL, spelled out: freshness imports this module.
    return [label for label in labels
            if not label.startswith(LEASE_LABEL_PREFIX) and label != "stale"]


BOARD_COMMENT_LIMIT = 12000
_COMMENT_BANNERS = (
    r"Reading additional input from stdin", r"(?:\*\*)?OpenAI Codex v",
    r"workdir: ", r"model: ", r"provider: ", r"approval: ", r"sandbox: ",
    r"reasoning effort: ", r"reasoning summaries: ",
    r"(?:\*\*)?session id: ", r"tokens used",
)


def comment_body(text, limit=BOARD_COMMENT_LIMIT):
    text = outbound(text)
    text = re.sub(
        r"(?m)^<!-- devin-review-badge-begin -->[^\n]*\n"
        r"[\s\S]*?^<!-- devin-review-badge-end -->[^\n]*(?:\n|$)",
        "", text,
    )
    text = re.sub(
        r"(?m)^(?:" + "|".join(_COMMENT_BANNERS) + r")[^\n]*(?:\n|$)",
        "", text,
    )
    text = re.sub(r"\n(?:[ \t]*\n){3,}", "\n\n\n", text)
    if len(text) <= limit:
        return text
    return (text[:limit] + f"\n[... {len(text) - limit} characters cut; "
            "the full round is on the run in the store]")


def lease_host(target):
    return host_label(target, socket.gethostname())


def lease_label(target):
    return LEASE_LABEL_PREFIX + lease_host(target)


def lease_turn_path(target):
    # Beside the store, never in the repository, where `git add -A` could commit it.
    return target.holo_dir / "lease.lock"


@contextlib.contextmanager
def lease_turn(target):
    # Held across a board call, so it is not the store's write lock. The file is
    # never unlinked: unlinking a flock file lets two holders exist.
    path = lease_turn_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def lease_turn_held(target):
    path = lease_turn_path(target)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def lease_holders(labels):
    return [str(label)[len(LEASE_LABEL_PREFIX):] for label in labels or []
            if str(label).startswith(LEASE_LABEL_PREFIX)]


def foreign_lease_holders(labels, host):
    return [holder for holder in lease_holders(labels) if holder != host]


def drop_lease_label(conn, ticket_id, provider, issue_id, label):
    try:
        provider.unlabel_issue(issue_id, label)
    except Exception as e:
        warn(conn, ticket_id, f"the lease label {label} could not be removed"
                              f" from {issue_id} ({e}); another writer will"
                              " refuse the ticket until it is")


def release_lease_label(target, conn, ticket_id, provider, run_id):
    if conn is None or provider is None:
        return
    # The label names the writer, not the run: a fresh claim may have re-asserted it.
    with lease_turn(target):
        ticket = store.read.ticket_by_id(conn, ticket_id)
        if ticket is None or ticket.activeRunId not in (None, run_id):
            return
        drop_lease_label(conn, ticket_id, provider, ticket.linearIssueId,
                         lease_label(target))


def mirror_key(task):
    return task.get("issue_id") or task["id"]


def on_pull_request(conn, project_id, task):
    row = conn.execute(
        "SELECT r.prUrl FROM tickets t JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.linearIssueId = ? AND t.projectId = ?",
        (mirror_key(task), project_id)).fetchone()
    return bool(row and row[0])


def store_status(conn, ticket_id):
    return store.read.ticket_by_id(conn, ticket_id).status


def task_contract(task):
    return (task["title"],
            list(task.get("criteria") or ()),
            [task["verify"]] if task.get("verify") else [],
            ticket_template.parse(task.get("body") or "").evidence_states)


def body_problems(task, repo=None, on_pull_request=False):
    body = task.get("body")
    if body is None:
        return []
    if on_pull_request:
        repo = None
    ticket = ticket_template.parse(body)
    problems = ticket_template.blocking(ticket_template.validate(ticket, repo=repo))
    if repo:
        from holophyte.pr_media import evidence_problems
        from holophyte.project import Project
        problems += evidence_problems(Project.locate(repo, adopt=False),
                                      ticket.evidence_states)
    return problems


def body_problem(task, repo=None, on_pull_request=False):
    problems = body_problems(task, repo, on_pull_request)
    return problems[0] if problems else None


VALIDATION_HEADING = "Not claimed: this ticket's body fails the template"


def note_problems(conn, ticket_id, kind, body, problems, text=None):
    if text is None:
        bullets = "\n".join(f"* {problem}" for problem in problems)
        text = f"**{VALIDATION_HEADING}**\n\n{bullets}"
    digest = hashlib.sha256(
        "\0".join([body or "", *problems]).encode()).hexdigest()
    return store.record_note(conn, ticket_id, kind, comment_body(text),
                             f"{kind}:{digest}")


def merge_drift(conn, run_id, provider, issue_id):
    # () means no evidence, not no drift: refusing on it would stall the queue.
    if conn is None or run_id is None or provider is None:
        return (), None
    fetch = getattr(provider, "fetch_task", None)
    if fetch is None:
        return (), None
    claimed = store.run_contract(conn, run_id)
    if claimed is None:
        return (), None
    try:
        live = fetch(issue_id)
    except Exception as e:
        warn_on_run(conn, run_id, f"could not re-read {issue_id} for the "
                                  f"merge-time drift check ({e}); merging on "
                                  "the contract frozen at the claim")
        return (), None
    if not live:
        warn_on_run(conn, run_id, f"{issue_id} could not be found for the "
                                  "merge-time drift check; merging on the "
                                  "contract frozen at the claim")
        return (), None
    return store.contract_drift(
        claimed, store.contract_snapshot(*task_contract(live))), live


def refresh_board_states(conn, project, provider):
    if project is None:
        return
    for ticket in store.read.open_tickets(conn, project):
        deadline.check(f"{ticket.linearIdentifier}'s board state")
        try:
            task = provider.fetch_task(ticket.linearIdentifier)
        except Exception as error:
            print(f"[holo2] board state refresh skipped {ticket.linearIdentifier}:"
                  f" {error}")
            continue
        state = task.get("board_state") if task else None
        if state is not None:
            with store.transaction(conn):
                store.set_board_state(conn, ticket.id, state)


def mirror_task(conn, project, task, specced=True, depends_on=None,
                column=None):
    title, criteria, commands, _states = task_contract(task)
    if column is None:
        column = task.get("column", "ready")
    if not specced:
        criteria, commands = [], []
    ticket_id = store.tickets.mirror_ticket(
        conn,
        project,
        linear_issue_id=mirror_key(task),
        linear_identifier=task["id"],
        title=title,
        acceptance_criteria=criteria,
        verification_commands=commands,
        time_box_ms=task["budget_min"] * 60 * 1000,
        body=task.get("body") or "",
        url=task.get("url"),
        board_state=task.get("board_state"),
        priority=task.get("priority"),
        labels=(None if task.get("labels") is None
                else board_owned_labels(task["labels"])),
        board_column=column,
        filed_at=task.get("filed_at"),
        board_updated_at=task.get("updatedAt"),
        # None keeps the stored dependencies; [] clears them.
        depends_on=depends_on,
        expected_revision=task.get("store_revision"),
    )
    if criteria and commands:
        with store.transaction(conn):
            ticket = store.read.ticket_by_id(conn, ticket_id)
            if ticket.status == "blocked_on_deps":
                store.tickets.walk_ticket(conn, ticket_id, "ready")
                if not store.tickets.pickable(conn, ticket_id):
                    store.tickets.walk_ticket(conn, ticket_id,
                                              "blocked_on_deps")
    return ticket_id


def release_run(conn, run_id, merged, reason=None, outcome_class="work",
                merge_sha=None, failure_kind=None):
    if merged:
        store.release(conn, run_id, "merged", merge_sha=merge_sha)
        return
    from holophyte.failure_reason import record

    record(conn, run_id, reason)
    store.release(conn, run_id, "failed", reason or
                  f"run stopped in phase {store.run_phase(conn, run_id)}",
                  outcome_class=outcome_class, failure_kind=failure_kind)


MIRROR_STATES = {
    "ready": "Todo",
    "in_flight": "In Progress",
    "merged": "Done",
    "abandoned": "Canceled",
    # No board state says "waiting on an operator"; Todo is where one looks.
    "blocked_on_operator": "Todo",
}
# needs_spec and blocked_on_deps are unmapped: their board state is left alone.


def warn(conn, ticket_id, summary):
    run_id = None
    if conn is not None:
        ticket = store.read.ticket_by_id(conn, ticket_id)
        if ticket is not None:
            run_id = (ticket.activeRunId if ticket.activeRunId is not None
                      else ticket.lastRunId)
    warn_on_run(conn, run_id, summary)


def mirror_push(conn, ticket_id, provider):
    if conn is None:
        return None
    ticket = store.read.ticket_by_id(conn, ticket_id)
    if ticket is None:
        raise ValueError(f"no ticket {ticket_id}")
    issue_id, identifier = ticket.linearIssueId, ticket.linearIdentifier
    status = ticket.status
    state = MIRROR_STATES.get(status)
    if state is None:
        return None
    # `is True`, not truthiness: a Mock board answers any attribute.
    if getattr(provider, "native", False) is True:
        return state
    if getattr(provider, "store_mode", False) is True:
        store.record_push(conn, ticket_id, state)
        return state
    try:
        provider.set_state(issue_id, state)
    except Exception as e:
        warn(conn, ticket_id, f"Linear mirror push failed for {identifier}: "
                              f"{status} -> {state} ({e}); the store keeps"
                              " the status and the board stays stale")
        return None
    return state


def mirror_status(conn, ticket_id, status, provider):
    if conn is None:
        return False
    try:
        store.tickets.transition(conn, ticket_id, status)
    except store.tickets.IllegalTransition as e:
        warn(conn, ticket_id, f"ticket status left where it was: {e}")
        return False
    mirror_push(conn, ticket_id, provider)
    return True


MAX_FAILED_RUNS = 2


def failure_history(conn, ticket_id):
    since = store.read.latest_human_intervention_at(conn, ticket_id)
    return [(run.attempt, run.outcomeReason) for run
            in store.read.failed_attempts_since(conn, ticket_id, since)]


def escalation_comment(history):
    lines = [f"**Blocked after {len(history)} failed runs.** Counted since"
             " the last recorded human intervention, if any; attempt numbers"
             " are lifetime. The factory will not claim this ticket again"
             " until a human moves it out of this state. What each counted"
             " attempt ended on:", ""]
    lines += [f"- attempt {attempt}: {reason or 'no reason recorded'}"
              for attempt, reason in history]
    return "\n".join(lines)


STRIKE_QUESTION_TAIL = (
    " runs failed on this ticket since the last recorded human intervention"
    " and the factory stopped claiming it; a human decides what happens next.")


def strike_question(count):
    return f"{count}{STRIKE_QUESTION_TAIL}"


def is_strike_question(question):
    return bool(question) and question.endswith(STRIKE_QUESTION_TAIL)


def escalate(conn, ticket_id, provider):
    if conn is None:
        return False
    ticket = store.read.ticket_by_id(conn, ticket_id)
    if ticket is None:
        return False
    status, issue_id = ticket.status, ticket.linearIssueId
    identifier = ticket.linearIdentifier
    if status == "blocked_on_operator":
        return True
    if status != "in_flight":
        return False
    history = failure_history(conn, ticket_id)
    if len(history) < MAX_FAILED_RUNS:
        return False
    question = strike_question(len(history))
    body = comment_body(escalation_comment(history))
    if getattr(provider, "store_mode", False) is True:
        # A park committed alone would return early above and never write the note.
        run_id = ticket.activeRunId or ticket.lastRunId
        with store.transaction(conn):
            if not block_ticket(conn, ticket_id, provider, question):
                return False
            store.record_note(conn, ticket_id, "escalation", body,
                              f"escalation:{run_id}", run_id=run_id)
    elif not block_ticket(conn, ticket_id, provider, question):
        return False
    else:
        try:
            provider.comment(issue_id, body)
        except Exception as e:
            warn(conn, ticket_id, f"failure history comment failed for "
                                  f"{identifier} ({e}); the store keeps the"
                                  " block and Linear is not told why")
    print(f"[holo2] {identifier} blocked after {len(history)} failed runs")
    return True


def block_ticket(conn, ticket_id, provider, question, park_kind="question"):
    if not mirror_status(conn, ticket_id, "blocked_on_operator", provider):
        return False
    store.set_question(conn, ticket_id, redact_values(question),
                       park_kind=park_kind)
    return True


def close_out_failure(target, conn, run_id, ticket_id, reason=None, provider=None,
                      confirm=None, outcome_class="work", refresh=True,
                      failure_kind=None):
    # release() stamps the failure before it frees the lease; the board calls
    # after it stay outside the write lock, which never spans a network call.
    with store.transaction(conn):
        if confirm is not None and not confirm():
            return False
        release_run(conn, run_id, False, reason, outcome_class,
                    failure_kind=failure_kind)
    cleanup_review_refs(target.path, run_id)
    escalate(conn, ticket_id, provider)
    release_lease_label(target, conn, ticket_id, provider, run_id)
    if refresh:
        refresh_findings(target, conn)
    return True


def ledger(conn, run_id, task_id, kind, text, provider):
    if conn is not None and run_id is not None:
        if getattr(provider, "store_mode", False) is True:
            with store.transaction(conn):
                entry = store.record_ledger(conn, run_id, kind, text)
                ticket_id = store.read.run_snapshot(conn, run_id).ticketId
                store.record_note(conn, ticket_id, "ledger",
                                  comment_body(redact_values(text)),
                                  f"ledger:{entry}", run_id=run_id)
            return
        store.record_ledger(conn, run_id, kind, text)
    else:
        print("[holo2] no store to record the ledger entry in")
    post_ledger_comment(task_id, text, provider)


def post_ledger_comment(task_id, text, provider):
    from datetime import datetime, timezone
    text = redact_values(text)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if provider is None:
        print("[holo2] no board to archive to; record kept in the store")
        return
    try:
        provider.comment(task_id, f"**{ts}**\n\n{comment_body(text)}")
    except Exception as e:
        print(f"[holo2] board comment failed ({e}); record kept in the store")


FILE_TICKET_PRIORITIES = {"urgent": 1, "high": 2, "medium": 3, "low": 4}

def _ticket_problems(text, repo):
    return store.board.ticket_problems(text, repo)


def file_ticket(target, path, state, board, out=None, priority=None,
                update=None, revision=None, labels=None):
    out = out or sys.stdout
    text = Path(path).read_text()
    ticket = ticket_template.parse(text)
    problems = _ticket_problems(text, target.path)
    if problems:
        print(f"[holo2] {path}: {problems[0]}", file=out)
        return 1
    refusals = (store.board.FilingRefused, store.RevisionMoved)
    if getattr(board, "native", False):
        refusals += (RuntimeError,)
    try:
        identifier = _file_or_update(board, ticket, text, state, priority,
                                     update, revision, labels, out)
    except store.RevisionMoved as moved:
        print(f"[holo2] {path}: {update} is at revision {moved.current}, not "
              f"{moved.expected}; nothing changed", file=out)
        return 1
    except refusals as refused:
        print(f"[holo2] {path}: {refused}", file=out)
        return 1
    # A client can rewrite a valid body in transfer, so the stored one is checked too.
    stored = _ticket_problems(board.stored_body(identifier), target.path)
    if stored:
        print(f"[holo2] {identifier}: as stored by Linear, {stored[0]}",
              file=out)
        return 2
    return 0


def _file_or_update(board, ticket, text, state, priority, update, revision,
                    labels, out):
    if update is not None:
        identifier = update
        given = {"revision": revision, "labels": labels,
                 "priority": FILE_TICKET_PRIORITIES[priority]
                 if priority else None}
        added, extra = board.update(
            update, ticket.title, text, ticket.estimate_min,
            ticket.depends_on or [],
            **{k: v for k, v in given.items() if v is not None})
        parts = []
        if added:
            parts.append("blocked by " + ", ".join(f"+{b}" for b in added))
        if extra:
            parts.append("board also holds " + ", ".join(extra))
        line = f"[holo2] updated {identifier}: {ticket.title}"
        if parts:
            line += f" ({'; '.join(parts)})"
        print(line, file=out)
    else:
        identifier = board.file(
            ticket.title, text, ticket.estimate_min, state,
            priority=FILE_TICKET_PRIORITIES[priority] if priority else None,
            blockers=ticket.depends_on or [])
        detail = f"{state}, {ticket.estimate_min} min"
        if ticket.depends_on:
            detail += f", blocked by {', '.join(ticket.depends_on)}"
        if priority:
            detail += f", {priority}"
        print(f"[holo2] filed {identifier}: {ticket.title} ({detail})",
              file=out)
    return identifier
