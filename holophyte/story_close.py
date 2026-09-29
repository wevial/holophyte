"""A story settles after each witness pass: closed at main's tip, or parked."""
from holophyte.board import mirror_push
from holophyte.gates import MergeLockHeld, merge_lock
from holophyte.redact import safe_print as print
from provider import board_for
from store.stories import (
    CLOSED_STATUSES,
    close_story,
    park_story,
    story,
    witness_ledger,
)

UNMET_OPTIONS = ("file a follow-up child", "accept the changed witness file",
                 "amend the witness (re-plan)", "abandon the story")
REGRESSED_OPTIONS = ("file a fix child", "rerun", "drop {key} (re-plan)")


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
              for row in witness_ledger(conn, found.ticketId, sha)}
    met = {witness.key for witness in found.witnesses
           if witness.key in latest and latest[witness.key].verdict == "green"
           and latest[witness.key].fileHash == witness.sourceHash}
    if met == {witness.key for witness in found.witnesses}:
        return "close"
    greens = {row.witnessKey for row in witness_ledger(conn, found.ticketId)
              if row.mainSha != sha and row.verdict == "green"}
    idle = not _open_children(conn, found.ticketId)
    decisions = []
    for witness in found.witnesses:
        row = latest.get(witness.key)
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
    latest = witness_ledger(conn, found.ticketId, sha)
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
