"""A story settles after each witness pass: closed at main's tip, or parked."""
import collections
import json
import re
import subprocess

from holophyte.board import mirror_push
from holophyte.gates import MergeLockHeld, merge_lock
from holophyte.redact import safe_print as print
from holophyte.story_claim import DRIFT_OPTIONS
from provider import board_for
from store.schema import transaction
from store.stories import (
    CLOSED_STATUSES,
    abandon_story,
    accept_witness,
    answer_decision,
    close_story,
    park_story,
    reapprove_edges,
    replan_story,
    story,
    witness_ledger,
)

UNMET_OPTIONS = ("file a follow-up child", "accept the changed witness file",
                 "amend the witness (re-plan)", "abandon the story")
REGRESSED_OPTIONS = ("file a fix child", "rerun", "drop {key} (re-plan)")
ACCEPT, ABANDON = UNMET_OPTIONS[1], UNMET_OPTIONS[3]
RERUN, REAPPROVE, REPLAN = REGRESSED_OPTIONS[1], DRIFT_OPTIONS[1], "(re-plan)"
WITNESS_KEY = re.compile(r"Witness (\S+) ")
GIT_TIMEOUT = 30


class DecisionRefused(Exception):
    pass


def settle_story(target, conn, story_id, sha):
    from holophyte.witness import main_tip
    if not settle_owed(conn, story_id, sha):
        return None
    try:
        with merge_lock(target, None):
            if main_tip(target) != sha:
                return None
            return _settle(target, conn, story_id, sha)
    except MergeLockHeld:
        return None


def settle_owed(conn, story_id, sha):
    return bool(_plan(conn, story(conn, story_id), sha))


def main_ledger(conn, story_id, sha=None):
    rows = [row for row in witness_ledger(conn, story_id)
            if row.verifier != "baseline"]
    if sha is None:
        return rows
    latest = {row.witnessKey: row for row in rows if row.mainSha == sha}
    return [latest[key] for key in sorted(latest)]


def rerun_owed(conn, story_id, sha):
    rows = main_ledger(conn, story_id)
    greens = {row.witnessKey for row in rows
              if row.mainSha != sha and row.verdict == "green"}
    runs = collections.Counter(row.witnessKey for row in rows
                               if row.mainSha == sha)
    latest = {row.witnessKey: row for row in rows if row.mainSha == sha}
    return {key for key, row in latest.items()
            if row.verdict == "red" and key in greens and runs[key] < 2}


def _settle(target, conn, story_id, sha):
    found = story(conn, story_id)
    plan = _plan(conn, found, sha)
    if plan == "close":
        return _close(target, conn, found, sha)
    for witness, kind, question, options in plan:
        park_story(conn, story_id, kind, question, options, options[0],
                   ticket_id=witness.completedBy)
        print(f"[holo2] story {_identifier(conn, story_id)} parked {kind}:"
              f" {question}")
    return story(conn, story_id).state


def _plan(conn, found, sha):
    latest = {row.witnessKey: row
              for row in main_ledger(conn, found.ticketId, sha)}
    met = {witness.key for witness in found.witnesses
           if witness.key in latest and latest[witness.key].verdict == "green"
           and latest[witness.key].fileHash == witness.sourceHash}
    if met == {witness.key for witness in found.witnesses}:
        return "close"
    greens = {row.witnessKey for row in main_ledger(conn, found.ticketId)
              if row.mainSha != sha and row.verdict == "green"}
    owed = rerun_owed(conn, found.ticketId, sha)
    idle = not _open_children(conn, found.ticketId)
    decisions = []
    for witness in found.witnesses:
        row = latest.get(witness.key)
        if witness.key in owed:
            continue
        if row is not None and row.verdict == "red" and witness.key in greens:
            decisions.append((
                witness, "regressed",
                f"Witness {witness.key} was green at an earlier commit and"
                f" is red at {sha} after its rerun.",
                [option.format(key=witness.key)
                 for option in REGRESSED_OPTIONS]))
        elif witness.key not in met and idle:
            decisions.append((
                witness, "unmet",
                f"Witness {witness.key} is {_unmet(row)} at {sha} and no"
                " child of the story is open or claimable.", UNMET_OPTIONS))
    return [decision for decision in decisions
            if not _already_open(found, *decision[:2])]


def _close(target, conn, found, sha):
    latest = main_ledger(conn, found.ticketId, sha)
    verdicts = ", ".join(f"{row.witnessKey} {row.verdict}" for row in latest)
    close_story(conn, found.ticketId, sha,
                f"Story closed on its witnesses at {sha}: {verdicts}.")
    board = board_for(target)
    if board is not None:
        mirror_push(conn, found.ticketId, board)
    print(f"[holo2] story {_identifier(conn, found.ticketId)} closed at {sha}:"
          f" {verdicts}")
    return "closed"


def _already_open(found, witness, kind):
    return any(decision.kind == kind
               and decision.question.startswith(f"Witness {witness.key} ")
               for decision in found.decisions)


def _unmet(row):
    if row is None:
        return "unwitnessed"
    if row.verdict == "green":
        return "green with its file changed from the approved source"
    return row.verdict


def _open_children(conn, story_id):
    return conn.execute(
        "SELECT 1 FROM tickets WHERE id IN (SELECT ticketId FROM storyChildren"
        " WHERE storyId = ?) AND status NOT IN (?, ?)"
        " AND COALESCE(boardColumn, '') NOT IN ('backlog', 'canceled')",
        (story_id, *CLOSED_STATUSES)).fetchone() is not None


def _identifier(conn, ticket_id):
    return conn.execute("SELECT linearIdentifier FROM tickets WHERE id = ?",
                        (ticket_id,)).fetchone()[0]


def decide(target, conn, identifier, decision_id, option, note):
    from holophyte.witness import pass_refusal, witness_pass
    story_id = _parent(conn, target, identifier)
    decision = _decision(conn, story_id, identifier, decision_id)
    answer = _chosen(decision, decision_id, option)
    refusal = pass_refusal(conn, story_id) if answer == RERUN else None
    if refusal is not None:
        raise DecisionRefused(f"no witness pass can rerun now: {refusal}")
    source = (_tip_source(target, conn, story_id, decision)
              if answer == ACCEPT else None)
    try:
        with transaction(conn):
            answer_decision(conn, decision_id, answer, "cli", note)
            _apply(conn, story_id, decision, answer, note, source)
    except ValueError as refused:
        raise DecisionRefused(str(refused)) from None
    lines = [f"decision {decision_id} of story {identifier}: {answer}"]
    if answer == ABANDON:
        board = board_for(target)
        if board is not None:
            mirror_push(conn, story_id, board)
    if answer == RERUN:
        rows = witness_pass(target, conn, story_id, "operator")
        if rows:
            lines.append(f"witness pass at {rows[0].mainSha}: " + ", ".join(
                f"{row.witnessKey} {row.verdict}" for row in rows))
    return [*lines, f"story {identifier} is {story(conn, story_id).state}"]


def _parent(conn, target, identifier):
    from holophyte.admission import project_of
    row = conn.execute("SELECT id FROM tickets WHERE projectId = ? AND"
                       " linearIdentifier = ?",
                       (project_of(conn, target), identifier)).fetchone()
    if row is None:
        raise DecisionRefused("no such ticket in this project")
    found = story(conn, row[0])
    if found is None or found.ticketId != row[0]:
        raise DecisionRefused("not a story's parent")
    return row[0]


def _decision(conn, story_id, identifier, decision_id):
    row = conn.execute(
        "SELECT ticketId, question, options, defaultOption, answer"
        " FROM storyDecisions WHERE id = ? AND storyId = ?",
        (decision_id, story_id)).fetchone()
    if row is None:
        raise DecisionRefused(f"story {identifier} holds no decision"
                              f" {decision_id}")
    if row[4] is not None:
        raise DecisionRefused(f"decision {decision_id} is already answered:"
                              f" {row[4]}")
    return {"ticketId": row[0], "question": row[1],
            "options": json.loads(row[2]), "default": row[3]}


def _chosen(decision, decision_id, option):
    if option is None or option == "default":
        return decision["default"]
    options = decision["options"]
    if not 1 <= int(option) <= len(options):
        raise DecisionRefused(f"decision {decision_id} has {len(options)}"
                              f" options; there is no option {option}")
    return options[int(option) - 1]


def _witness(conn, story_id, decision):
    matched = WITNESS_KEY.match(decision["question"])
    found = story(conn, story_id)
    for witness in found.witnesses:
        if matched and witness.key == matched.group(1):
            return witness
    raise DecisionRefused("the decision names no witness of the story")


def _tip_source(target, conn, story_id, decision):
    from holophyte.witness import main_tip
    witness = _witness(conn, story_id, decision)
    try:
        shown = subprocess.run(
            ["git", "show", f"{main_tip(target)}:{witness.file}"],
            cwd=target.path, capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise DecisionRefused(f"main's tip could not be read: {error}") from None
    if shown.returncode != 0:
        raise DecisionRefused(f"{witness.file} is not at main's tip")
    try:
        return witness.key, shown.stdout.decode()
    except UnicodeDecodeError:
        raise DecisionRefused(f"{witness.file} at main's tip is not"
                              " UTF-8 text") from None


def _apply(conn, story_id, decision, answer, note, source):
    if answer == ABANDON:
        abandon_story(conn, story_id, note, "cli")
    elif answer == ACCEPT:
        accept_witness(conn, story_id, *source)
    elif answer == REAPPROVE:
        reapprove_edges(conn, story_id, decision["ticketId"])
    elif answer.endswith(REPLAN):
        _replan(conn, story_id)


def _replan(conn, story_id):
    found = story(conn, story_id)
    witnesses = [witness._asdict() for witness in found.witnesses]
    keys = collections.defaultdict(list)
    roles = {}
    for child in found.children:
        roles[child.ticketId] = child.role
        if child.witnessKey:
            keys[child.ticketId].append(child.witnessKey)
    replan_story(conn, story_id, witnesses,
                 [(ticket_id, role, keys[ticket_id])
                  for ticket_id, role in roles.items()],
                 found.standingOrders)
