"""A story child's follow-up proposed as a new child, held outside the plan."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .follow_ups import _settle
from .operate import record_project_intervention
from .schema import _transaction

PROPOSABLE_STATES = ("planned", "approved", "parked")


@dataclass(frozen=True)
class Proposal:
    id: int
    raisedBy: str
    text: str


def _key(proposal_id):
    return f"p{proposal_id}"


def record_proposal(conn, story_id, follow_up_id, raised_by, title, body,
                    now=None):
    """The proposal's id; its follow-up row is settled with no draft filed."""
    now = int(time.time() * 1000) if now is None else now
    with _transaction(conn):
        row = conn.execute("SELECT state FROM stories WHERE ticketId = ?",
                           (story_id,)).fetchone()
        if row is None:
            raise ValueError(f"ticket {story_id} has no story")
        if row[0] not in PROPOSABLE_STATES:
            raise ValueError(f"story {story_id} is {row[0]}")
        if conn.execute("SELECT 1 FROM storyChildren WHERE storyId = ?"
                        " AND ticketId = ?",
                        (story_id, raised_by)).fetchone() is None:
            raise ValueError(f"ticket {raised_by} is not a child of story"
                             f" {story_id}")
        proposal_id = conn.execute(
            "INSERT INTO storyProposals (storyId, followUpId, raisedBy, title,"
            " body, state, at) VALUES (?, ?, ?, ?, ?, 'proposed', ?)",
            (story_id, follow_up_id, raised_by, title, body, now)).lastrowid
        (story_key,) = conn.execute(
            "SELECT linearIdentifier FROM tickets WHERE id = ?",
            (story_id,)).fetchone()
        key = _key(proposal_id)
        if not _settle(conn, follow_up_id, "follow_up_proposed",
                       f"proposed as {key} of story {story_key}", now,
                       named=key):
            raise ValueError(f"follow-up {follow_up_id} is already settled")
    return proposal_id


def accept_proposal(conn, proposal_id, child_id, author, note, now=None):
    now = int(time.time() * 1000) if now is None else now
    with _transaction(conn):
        story_id, project_id = _decidable(conn, proposal_id)
        row = conn.execute("SELECT linearIdentifier, dependsOn, parentTicketId"
                           " FROM tickets WHERE id = ? AND projectId = ?",
                           (child_id, project_id)).fetchone()
        if row is None:
            raise ValueError(f"ticket {child_id} is not in the story's"
                             " project")
        child_key, depends, parent = row
        if parent is not None or conn.execute(
                "SELECT 1 FROM storyChildren WHERE ticketId = ?",
                (child_id,)).fetchone() is not None:
            raise ValueError(f"ticket {child_key} already serves a story")
        conn.execute("INSERT INTO storyChildren (ticketId, witnessKey,"
                     " storyId, role) VALUES (?, '', ?, 'scaffolding')",
                     (child_id, story_id))
        conn.execute("UPDATE tickets SET parentTicketId = ? WHERE id = ?",
                     (story_id, child_id))
        (plan,) = conn.execute("SELECT approvedPlan FROM stories"
                               " WHERE ticketId = ?", (story_id,)).fetchone()
        if plan is not None:
            plan = json.loads(plan)
            plan["children"].append({"identifier": child_key,
                                     "role": "scaffolding",
                                     "witnessKeys": []})
            plan["edges"][child_key] = json.loads(depends)
            conn.execute("UPDATE stories SET approvedPlan = ?"
                         " WHERE ticketId = ?", (json.dumps(plan), story_id))
        _decide(conn, proposal_id, "accepted", child_id, author, note,
                project_id, now)


def reject_proposal(conn, proposal_id, author, note, now=None):
    now = int(time.time() * 1000) if now is None else now
    with _transaction(conn):
        _, project_id = _decidable(conn, proposal_id)
        _decide(conn, proposal_id, "rejected", None, author, note, project_id,
                now)


def _decidable(conn, proposal_id):
    row = conn.execute(
        "SELECT p.storyId, p.state, s.state, t.projectId FROM storyProposals p"
        " JOIN stories s ON s.ticketId = p.storyId"
        " JOIN tickets t ON t.id = p.storyId WHERE p.id = ?",
        (proposal_id,)).fetchone()
    if row is None:
        raise ValueError(f"no proposal {_key(proposal_id)}")
    story_id, state, story_state, project_id = row
    if story_state not in PROPOSABLE_STATES:
        raise ValueError(f"story {story_id} of proposal {_key(proposal_id)}"
                         f" is {story_state}")
    if state != "proposed":
        raise ValueError(f"proposal {_key(proposal_id)} is already {state}")
    return story_id, project_id


def _decide(conn, proposal_id, state, child_id, author, note, project_id,
            now):
    record_project_intervention(conn, "decide", note, source="human",
                                trigger="manual", project_id=project_id,
                                now=now)
    conn.execute("UPDATE storyProposals SET state = ?, childTicketId = ?,"
                 " decidedBy = ?, decidedAt = ? WHERE id = ?",
                 (state, child_id, author, now, proposal_id))


def open_proposals(conn, story_id):
    return [Proposal(*row) for row in conn.execute(
        "SELECT p.id, t.linearIdentifier, f.text FROM storyProposals p"
        " JOIN tickets t ON t.id = p.raisedBy"
        " JOIN followUps f ON f.id = p.followUpId"
        " WHERE p.storyId = ? AND p.state = 'proposed' ORDER BY p.id",
        (story_id,))]


def story_duplicate(conn, story_id, fingerprint, text, normalise):
    """`("child", KEY-n)` or `("proposal", pN)` the follow-up repeats, else None."""
    for key, title, body in conn.execute(
            "SELECT linearIdentifier, title, COALESCE(body, '') FROM tickets"
            " WHERE id IN (SELECT ticketId FROM storyChildren"
            " WHERE storyId = ?) AND status != 'abandoned' ORDER BY id",
            (story_id,)):
        if (f"Follow-up fingerprint: {fingerprint}" in body
                or normalise(title) == normalise(text)):
            return "child", key
    row = conn.execute(
        "SELECT p.id FROM storyProposals p JOIN followUps f"
        " ON f.id = p.followUpId WHERE p.storyId = ? AND f.fingerprint = ?"
        " ORDER BY p.id LIMIT 1", (story_id, fingerprint)).fetchone()
    return None if row is None else ("proposal", _key(row[0]))
