"""A story's proposed child answered: filed in Backlog as its child, or rejected."""
import re

import ticket_template
from holophyte.board.projection import mirror_task
from holophyte.story.story_close import DecisionRefused
from provider import FiledWithoutBlockers, board_for
from store.schema import transaction
from store.stories import story
from store.story_proposals import (
    PROPOSABLE_STATES,
    accept_proposal,
    reject_proposal,
)

PROPOSAL_OPTIONS = ("accept the proposed child", "reject the proposed child")
ACCEPT = PROPOSAL_OPTIONS[0]
BACKLOG = "Backlog"
DEPENDS_ON = re.compile(r"^(Estimate:.*?Depends on:).*$", re.MULTILINE)
ESTIMATE_HEADING = "## Estimate & dependencies"
STORY_SECTION = "## Story\n\nRole: scaffolding\n\n"


def decide(target, conn, identifier, story_id, key, option, note):
    proposal = _proposal(conn, story_id, identifier, key)
    answer = _chosen(key, option)
    filed = None
    if answer == ACCEPT:
        filed = _accept(target, conn, proposal, note)
    else:
        try:
            reject_proposal(conn, proposal["id"], "cli", note)
        except ValueError as refused:
            raise DecisionRefused(str(refused)) from None
    lines = [f"proposal {key} of story {identifier}: {answer}"]
    if filed is not None:
        lines.append(f"child {filed} filed in {BACKLOG}")
    return [*lines, f"story {identifier} is {story(conn, story_id).state}"]


def _proposal(conn, story_id, identifier, key):
    row = conn.execute(
        "SELECT p.id, p.state, p.title, p.body, t.linearIdentifier,"
        " t.linearIssueId, s.linearIssueId, s.projectId FROM storyProposals p"
        " JOIN tickets t ON t.id = p.raisedBy JOIN tickets s"
        " ON s.id = p.storyId WHERE p.id = ? AND p.storyId = ?",
        (int(key[1:]), story_id)).fetchone()
    if row is None:
        raise DecisionRefused(f"story {identifier} holds no proposal {key}")
    state = story(conn, story_id).state
    if state not in PROPOSABLE_STATES:
        raise DecisionRefused(f"story {identifier} is {state}")
    if row[1] != "proposed":
        raise DecisionRefused(f"proposal {key} is already {row[1]}")
    return dict(zip(("id", "state", "title", "body", "raiser", "raiserIssue",
                     "parentIssue", "projectId"), row))


def _chosen(key, option):
    if option is None or option == "default":
        return PROPOSAL_OPTIONS[0]
    if not 1 <= int(option) <= len(PROPOSAL_OPTIONS):
        raise DecisionRefused(f"proposal {key} has {len(PROPOSAL_OPTIONS)}"
                              f" options; there is no option {option}")
    return PROPOSAL_OPTIONS[int(option) - 1]


def _child_body(body, raiser):
    body = DEPENDS_ON.sub(lambda match: f"{match[1]} {raiser}", body, count=1)
    if ESTIMATE_HEADING not in body:
        return f"{body.rstrip()}\n\n{STORY_SECTION}"
    return body.replace(ESTIMATE_HEADING, STORY_SECTION + ESTIMATE_HEADING, 1)


def _accept(target, conn, proposal, note):
    board = board_for(target)
    if board is None:
        raise DecisionRefused("the project has no board to file the child on")
    body = _child_body(proposal["body"], proposal["raiser"])
    estimate = ticket_template.parse(body).estimate_min
    native = getattr(board, "native", False)
    try:
        filed = board.file(proposal["title"], body, estimate, BACKLOG,
                           blockers=[] if native else [proposal["raiser"]],
                           parent=None if native else proposal["parentIssue"])
    except FiledWithoutBlockers as refused:
        raise DecisionRefused(f"the board refused the child: {refused};"
                              f" already created, to cancel on the board:"
                              f" {refused.identifier}") from None
    except Exception as refused:
        raise DecisionRefused(f"the board refused the child: {refused}"
                              ) from None
    try:
        task = None if native else board.fetch_task(filed)
        with transaction(conn):
            child_id = (_ticket_id(conn, proposal, filed) if native
                        else mirror_task(
                            conn, proposal["projectId"], task, specced=False,
                            depends_on=[proposal["raiserIssue"]]))
            accept_proposal(conn, proposal["id"], child_id, "cli", note)
    except Exception as refused:
        raise DecisionRefused(f"the child was not stored: {refused};"
                              f" already created, to cancel on the board:"
                              f" {filed}") from None
    return filed


def _ticket_id(conn, proposal, filed):
    (ticket_id,) = conn.execute(
        "SELECT id FROM tickets WHERE projectId = ? AND linearIdentifier = ?",
        (proposal["projectId"], filed)).fetchone()
    return ticket_id
