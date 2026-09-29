"""A story's rows: filed, read, approved, witnessed, parked, closed, abandoned."""
import collections
import hashlib
import json
import time

from .enums import ChildRole, DecisionKind, RedKind, WitnessVerdict, WitnessVerifier
from .notes import record_note
from .operate import record_project_intervention
from .revisions import record_board_fields
from .schema import _transaction
from .tickets import walk_ticket
from .writes import set_board_state

STORY_COLUMNS = ("ticketId", "state", "generation", "standingOrders",
                 "approvedRevision", "approvedPlan", "approvedBy", "approvedAt",
                 "closedSha", "closedAt")
WITNESS_COLUMNS = ("key", "criterion", "file", "command", "source",
                   "sourceHash", "completedBy")
CHILD_COLUMNS = ("ticketId", "witnessKey", "role")
WITNESS_FIELDS = ("key", "criterion", "file", "command", "source")
RESULT_COLUMNS = ("id", "witnessKey", "mainSha", "verdict", "redKind",
                  "verifier", "fileHash", "evidencePath", "seconds", "at")
DECISION_COLUMNS = ("id", "kind", "ticketId", "question", "options",
                    "defaultOption")

Story = collections.namedtuple(
    "Story", (*STORY_COLUMNS, "witnesses", "children", "decisions"))
StoryWitness = collections.namedtuple("StoryWitness", WITNESS_COLUMNS)
StoryChild = collections.namedtuple("StoryChild", CHILD_COLUMNS)
StoryDecision = collections.namedtuple("StoryDecision", DECISION_COLUMNS)
WitnessResult = collections.namedtuple("WitnessResult", RESULT_COLUMNS)

OPEN_STATES = ("approved", "parked")
CLOSED_STATUSES = ("merged", "abandoned")


def file_story(conn, parent_id, witnesses, children, standing_orders=(),
               now=None):
    witnesses = [dict(witness) for witness in witnesses]
    children = [(ticket_id, role, tuple(keys))
                for ticket_id, role, keys in children]
    keys = _check_witnesses(witnesses)
    completed_by = _check_children(parent_id, children, keys)
    orders = list(standing_orders)
    if not all(isinstance(order, str) and order.strip() for order in orders):
        raise ValueError("a standing order is blank")
    with _transaction(conn):
        _check_tickets(conn, parent_id, [child[0] for child in children])
        conn.execute("INSERT INTO stories (ticketId, state, generation,"
                     " standingOrders) VALUES (?, 'planned', 0, ?)",
                     (parent_id, json.dumps(orders)))
        for witness in witnesses:
            conn.execute(
                "INSERT INTO storyWitnesses (storyId, key, criterion, file,"
                " command, source, sourceHash, completedBy)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (parent_id, *(witness[field] for field in WITNESS_FIELDS),
                 hashlib.sha256(witness["source"].encode()).hexdigest(),
                 completed_by.get(witness["key"])))
        for ticket_id, role, child_keys in children:
            for key in child_keys or ("",):
                conn.execute("INSERT INTO storyChildren (ticketId, witnessKey,"
                             " storyId, role) VALUES (?, ?, ?, ?)",
                             (ticket_id, key, parent_id, role))
            conn.execute("UPDATE tickets SET parentTicketId = ? WHERE id = ?",
                         (parent_id, ticket_id))


def story(conn, ticket_id):
    row = conn.execute(
        f"SELECT {', '.join(STORY_COLUMNS)} FROM stories WHERE ticketId = ?"
        " OR ticketId = (SELECT storyId FROM storyChildren WHERE ticketId = ?"
        " LIMIT 1)", (ticket_id, ticket_id)).fetchone()
    if row is None:
        return None
    fields = dict(zip(STORY_COLUMNS, row))
    fields["standingOrders"] = tuple(json.loads(fields["standingOrders"]))
    if fields["approvedPlan"] is not None:
        fields["approvedPlan"] = json.loads(fields["approvedPlan"])
    witnesses = tuple(StoryWitness(*witness) for witness in conn.execute(
        f"SELECT {', '.join(WITNESS_COLUMNS)} FROM storyWitnesses"
        " WHERE storyId = ? ORDER BY key", (fields["ticketId"],)))
    children = tuple(StoryChild(*child) for child in conn.execute(
        f"SELECT {', '.join(CHILD_COLUMNS)} FROM storyChildren"
        " WHERE storyId = ? ORDER BY ticketId, witnessKey",
        (fields["ticketId"],)))
    decisions = tuple(
        StoryDecision(*decision[:4], tuple(json.loads(decision[4])),
                      decision[5]) for decision in conn.execute(
            f"SELECT {', '.join(DECISION_COLUMNS)} FROM storyDecisions"
            " WHERE storyId = ? AND answer IS NULL ORDER BY id",
            (fields["ticketId"],)))
    return Story(**fields, witnesses=witnesses, children=children,
                 decisions=decisions)


def approve_story(conn, parent_id, revision, author, note, now=None):
    _check_text(author=author, note=note)
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        state = _story_state(conn, parent_id)
        if state != "planned":
            raise ValueError(f"story {parent_id} is {state}, not planned")
        project_id, current = conn.execute(
            "SELECT projectId, revision FROM tickets WHERE id = ?",
            (parent_id,)).fetchone()
        if current != revision:
            raise ValueError(f"story {parent_id}'s parent is at revision"
                             f" {current}, not {revision}")
        other = conn.execute(
            "SELECT s.ticketId, s.state FROM stories s"
            " JOIN tickets t ON t.id = s.ticketId WHERE t.projectId = ?"
            " AND s.ticketId != ? AND s.state IN (?, ?) LIMIT 1",
            (project_id, parent_id, *OPEN_STATES)).fetchone()
        if other is not None:
            raise ValueError(f"story {other[0]} of the same project is"
                             f" {other[1]}; one story is open at a time")
        conn.execute(
            "UPDATE stories SET state = 'approved', approvedRevision = ?,"
            " approvedPlan = ?, approvedBy = ?, approvedAt = ?"
            " WHERE ticketId = ?",
            (revision, json.dumps(_plan(conn, parent_id)), author, now,
             parent_id))
        return record_project_intervention(
            conn, "approve_story", note, source="human", trigger="manual",
            project_id=project_id, now=now)


def abandon_story(conn, parent_id, note, author, now=None):
    _check_text(note=note, author=author)
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        state = _story_state(conn, parent_id)
        if state in ("closed", "abandoned"):
            raise ValueError(f"story {parent_id} is already {state}")
        conn.execute("UPDATE stories SET state = 'abandoned'"
                     " WHERE ticketId = ?", (parent_id,))
        _supersede_decisions(conn, parent_id, "abandon_story", now)
        (status,) = conn.execute("SELECT status FROM tickets WHERE id = ?",
                                 (parent_id,)).fetchone()
        if status not in CLOSED_STATUSES:
            walk_ticket(conn, parent_id, "abandoned")
        _backlog_unclaimed(conn, parent_id, note, author, now)


def record_witness_result(conn, story_id, key, main_sha, verdict, verifier,
                          red_kind=None, file_hash=None, evidence_path=None,
                          seconds=None, now=None):
    _check_text(main_sha=main_sha)
    _check_member("verdict", verdict, WitnessVerdict)
    _check_member("verifier", verifier, WitnessVerifier)
    if verdict == "red":
        _check_member("redKind", red_kind, RedKind)
    elif red_kind is not None:
        raise ValueError(f"a {verdict} verdict has no redKind")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        _story_state(conn, story_id)
        if conn.execute("SELECT 1 FROM storyWitnesses WHERE storyId = ?"
                        " AND key = ?", (story_id, key)).fetchone() is None:
            raise ValueError(f"story {story_id} has no witness {key!r}")
        return conn.execute(
            "INSERT INTO witnessResults (storyId, witnessKey, mainSha,"
            " verdict, redKind, verifier, fileHash, evidencePath, seconds, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (story_id, key, main_sha, verdict, red_kind, verifier, file_hash,
             evidence_path, seconds, now)).lastrowid


def witness_ledger(conn, story_id, main_sha=None):
    columns = ", ".join(RESULT_COLUMNS)
    if main_sha is None:
        rows = conn.execute(f"SELECT {columns} FROM witnessResults"
                            " WHERE storyId = ? ORDER BY id", (story_id,))
    else:
        rows = conn.execute(
            f"SELECT {columns} FROM witnessResults WHERE id IN"
            " (SELECT MAX(id) FROM witnessResults WHERE storyId = ?"
            " AND mainSha = ? GROUP BY witnessKey) ORDER BY witnessKey",
            (story_id, main_sha))
    return [WitnessResult(*row) for row in rows]


def park_story(conn, story_id, kind, question, options, default_option,
               ticket_id=None, now=None):
    options = list(options)
    _check_member("kind", kind, DecisionKind)
    _check_text(question=question)
    if not options:
        raise ValueError("a decision lists no options")
    for option in options:
        _check_text(option=option)
    if len(set(options)) != len(options):
        raise ValueError("a decision lists an option twice")
    if default_option not in options:
        raise ValueError(f"default {default_option!r} is not among the"
                         " options")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        state = _story_state(conn, story_id)
        if state not in OPEN_STATES:
            raise ValueError(f"story {story_id} is {state}, not approved or"
                             " parked")
        if ticket_id is not None and conn.execute(
                "SELECT 1 FROM storyChildren WHERE storyId = ?"
                " AND ticketId = ?", (story_id, ticket_id)).fetchone() is None:
            raise ValueError(f"ticket {ticket_id} is not a child of story"
                             f" {story_id}")
        conn.execute("UPDATE stories SET state = 'parked' WHERE ticketId = ?",
                     (story_id,))
        return conn.execute(
            "INSERT INTO storyDecisions (storyId, ticketId, kind, question,"
            " options, defaultOption, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (story_id, ticket_id, kind, question, json.dumps(options),
             default_option, now)).lastrowid


def answer_decision(conn, decision_id, answer, author, note, now=None):
    _check_text(author=author, note=note)
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute("SELECT storyId, options, answer FROM"
                           " storyDecisions WHERE id = ?",
                           (decision_id,)).fetchone()
        if row is None:
            raise ValueError(f"no decision {decision_id}")
        story_id, options, answered = row
        state = _story_state(conn, story_id)
        if state in ("closed", "abandoned"):
            raise ValueError(f"decision {decision_id}'s story {story_id} is"
                             f" {state}")
        if answered is not None:
            raise ValueError(f"decision {decision_id} is already answered"
                             f" {answered!r}")
        if answer not in json.loads(options):
            raise ValueError(f"{answer!r} is not an option of decision"
                             f" {decision_id}")
        conn.execute("UPDATE storyDecisions SET answer = ?, answeredBy = ?,"
                     " answeredAt = ? WHERE id = ?",
                     (answer, author, now, decision_id))
        (project_id,) = conn.execute("SELECT projectId FROM tickets"
                                     " WHERE id = ?", (story_id,)).fetchone()
        record_project_intervention(conn, "decide", note, source="human",
                                    trigger="manual", project_id=project_id,
                                    now=now)
        pending = conn.execute("SELECT 1 FROM storyDecisions WHERE storyId = ?"
                               " AND answer IS NULL", (story_id,)).fetchone()
        if pending is None:
            conn.execute("UPDATE stories SET state = 'approved'"
                         " WHERE ticketId = ? AND state = 'parked'",
                         (story_id,))
        return _story_state(conn, story_id)


def close_story(conn, story_id, main_sha, note, now=None):
    _check_text(main_sha=main_sha, note=note)
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        state = _story_state(conn, story_id)
        if state not in OPEN_STATES:
            raise ValueError(f"story {story_id} is {state}, not approved or"
                             " parked")
        conn.execute("UPDATE stories SET state = 'closed', closedSha = ?,"
                     " closedAt = ? WHERE ticketId = ?",
                     (main_sha, now, story_id))
        _supersede_decisions(conn, story_id, "close_story", now)
        walk_ticket(conn, story_id, "merged")
        record_note(conn, story_id, "verdict", note,
                    f"story-closed:{story_id}:{main_sha}", now=now)
        _backlog_unclaimed(conn, story_id,
                           f"story closed on its witnesses at {main_sha}",
                           "factory", now)


def _supersede_decisions(conn, story_id, operation, now):
    conn.execute("UPDATE storyDecisions SET answer = 'superseded',"
                 " answeredBy = ?, answeredAt = ? WHERE storyId = ?"
                 " AND answer IS NULL", (operation, now, story_id))


def _backlog_unclaimed(conn, parent_id, note, author, now):
    unclaimed = conn.execute(
        "SELECT t.id FROM tickets t WHERE t.id IN (SELECT ticketId"
        " FROM storyChildren WHERE storyId = ?)"
        " AND t.status NOT IN (?, ?)"
        " AND COALESCE(t.boardColumn, '') NOT IN ('backlog', 'canceled')"
        " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticketId = t.id"
        " AND r.endedAt IS NULL) ORDER BY t.id",
        (parent_id, *CLOSED_STATUSES)).fetchall()
    for (ticket_id,) in unclaimed:
        set_board_state(conn, ticket_id, "Backlog", "backlog")
        revision = record_board_fields(conn, ticket_id, author, now)
        record_note(conn, ticket_id, "move", note, f"move:{revision}",
                    author=author, now=now)


def _check_member(name, value, enum):
    values = [member.value for member in enum]
    if value not in values:
        raise ValueError(f"{name} {value!r} is not one of {', '.join(values)}")


def _check_text(**values):
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is blank")


def _check_witnesses(witnesses):
    keys = set()
    for witness in witnesses:
        for field in WITNESS_FIELDS:
            if not isinstance(witness.get(field), str) or not witness[field].strip():
                raise ValueError(f"a witness's {field} is blank")
        if witness["key"] in keys:
            raise ValueError(f"witness key {witness['key']} is given twice")
        keys.add(witness["key"])
    return keys


def _check_children(parent_id, children, keys):
    """Refuse a child the story cannot hold; answer each witness's completer."""
    roles = [member.value for member in ChildRole]
    seen, completed_by = set(), {}
    for ticket_id, role, child_keys in children:
        if ticket_id == parent_id:
            raise ValueError(f"child {ticket_id} is the parent")
        if ticket_id in seen:
            raise ValueError(f"child {ticket_id} is given twice")
        seen.add(ticket_id)
        if role not in roles:
            raise ValueError(f"role {role!r} is not one of {', '.join(roles)}")
        if role == "scaffolding" and child_keys:
            raise ValueError(f"child {ticket_id}: scaffolding names no witness")
        if role != "scaffolding" and not child_keys:
            raise ValueError(f"child {ticket_id}: {role} names a witness")
        repeated = sorted({key for key in child_keys
                           if child_keys.count(key) > 1})
        if repeated:
            raise ValueError(f"child {ticket_id} names {repeated[0]} twice")
        for key in child_keys:
            if key not in keys:
                raise ValueError(f"child {ticket_id}: {role} {key} names no"
                                 " witness")
            if role == "completes" and key in completed_by:
                raise ValueError(f"witness {key} is completed by two children")
            if role == "completes":
                completed_by[key] = ticket_id
    return completed_by


def _check_tickets(conn, parent_id, child_ids):
    rows = {}
    for ticket_id in (parent_id, *child_ids):
        rows[ticket_id] = conn.execute(
            "SELECT projectId, parentTicketId FROM tickets WHERE id = ?",
            (ticket_id,)).fetchone()
        if rows[ticket_id] is None:
            raise ValueError(f"ticket {ticket_id!r} is not in the store")
    if _has_story(conn, parent_id):
        raise ValueError(f"ticket {parent_id} already has a story")
    project_id = rows[parent_id][0]
    for ticket_id in child_ids:
        child_project, parent = rows[ticket_id]
        if child_project != project_id:
            raise ValueError(f"child {ticket_id} is in project {child_project},"
                             f" not the parent's project {project_id}")
        if parent is not None:
            raise ValueError(f"child {ticket_id} already serves story {parent}")
        if _has_story(conn, ticket_id):
            raise ValueError(f"child {ticket_id} already owns a story")


def _has_story(conn, ticket_id):
    return conn.execute("SELECT 1 FROM stories WHERE ticketId = ?",
                        (ticket_id,)).fetchone() is not None


def _story_state(conn, parent_id):
    row = conn.execute("SELECT state FROM stories WHERE ticketId = ?",
                       (parent_id,)).fetchone()
    if row is None:
        raise ValueError(f"ticket {parent_id} has no story")
    return row[0]


def _plan(conn, parent_id):
    rows = conn.execute(
        "SELECT t.linearIdentifier, c.role, c.witnessKey, t.dependsOn"
        " FROM storyChildren c JOIN tickets t ON t.id = c.ticketId"
        " WHERE c.storyId = ? ORDER BY t.id, c.witnessKey",
        (parent_id,)).fetchall()
    children, edges = {}, {}
    for identifier, role, key, depends in rows:
        child = children.setdefault(
            identifier, {"identifier": identifier, "role": role,
                         "witnessKeys": []})
        if key:
            child["witnessKeys"].append(key)
        edges[identifier] = json.loads(depends)
    return {"children": list(children.values()), "edges": edges}
