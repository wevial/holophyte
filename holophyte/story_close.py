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
    try:
        with merge_lock(target, None):
            if main_tip(target) != sha:
                return None
            return _settle(target, conn, story_id, sha)
    except MergeLockHeld:
        return None


def _settle(target, conn, story_id, sha):
    found = story(conn, story_id)
    latest = {row.witnessKey: row for row in witness_ledger(conn, story_id, sha)}
    met = {witness.key for witness in found.witnesses
           if witness.key in latest and latest[witness.key].verdict == "green"
           and latest[witness.key].fileHash == witness.sourceHash}
    if met == {witness.key for witness in found.witnesses}:
        return _close(target, conn, found, sha, latest)
    greens = {row.witnessKey for row in witness_ledger(conn, story_id)
              if row.mainSha != sha and row.verdict == "green"}
    idle = not _open_children(conn, story_id)
    for witness in found.witnesses:
        row = latest.get(witness.key)
        if row is not None and row.verdict == "red" and witness.key in greens:
            _park(conn, found, witness, "regressed",
                  f"Witness {witness.key} was green at an earlier commit and"
                  f" is red at {sha} after its rerun.",
                  [option.format(key=witness.key)
                   for option in REGRESSED_OPTIONS])
        elif witness.key not in met and idle:
            _park(conn, found, witness, "unmet",
                  f"Witness {witness.key} is {_unmet(row)} at {sha} and no"
                  " child of the story is open or claimable.", UNMET_OPTIONS)
    return story(conn, story_id).state


def _close(target, conn, found, sha, latest):
    verdicts = ", ".join(f"{key} {latest[key].verdict}" for key in sorted(latest))
    close_story(conn, found.ticketId, sha,
                f"Story closed on its witnesses at {sha}: {verdicts}.")
    board = board_for(target)
    if board is not None:
        mirror_push(conn, found.ticketId, board)
    print(f"[holo2] story {_identifier(conn, found.ticketId)} closed at {sha}:"
          f" {verdicts}")
    return "closed"


def _park(conn, found, witness, kind, question, options):
    if any(decision.kind == kind
           and decision.question.startswith(f"Witness {witness.key} ")
           for decision in found.decisions):
        return
    park_story(conn, found.ticketId, kind, question, options, options[0],
               ticket_id=witness.completedBy)
    print(f"[holo2] story {_identifier(conn, found.ticketId)} parked {kind}:"
          f" {question}")


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
