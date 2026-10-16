from __future__ import annotations

import re

import store.read
from holophyte.serve.serve_runs import no_store
from store.stories import story, story_children, story_decisions, witness_ledger

STORY_PATH = re.compile(r"^/stories/([^/]+)$")


def story_detail(project, identifier):
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        ticket = store.read.ticket_by_identifier(conn, identifier)
        found = None if ticket is None else story(conn, ticket.id)
        if found is None or found.ticketId != ticket.id:
            return 404, {}
        children = story_children(conn, ticket.id)
        ledger = witness_ledger(conn, ticket.id)
        latest = ({row.witnessKey: row for row in witness_ledger(
            conn, ticket.id, ledger[-1].mainSha)} if ledger else {})
        decisions = story_decisions(conn, ticket.id)
    finally:
        conn.close()
    return 200, {"ticket": ticket.linearIdentifier, "title": ticket.title,
                 "state": found.state, "generation": found.generation,
                 "children": [child_json(child) for child in children],
                 "witnesses": [witness_json(witness, latest.get(witness.key))
                               for witness in found.witnesses],
                 "decisions": [decision_json(decision)
                               for decision in decisions]}


def child_json(child):
    return {"ticket": child.identifier, "title": child.title,
            "status": child.status, "column": child.boardColumn,
            "time_box_ms": child.timeBoxMs, "run": child.activeRunId,
            "merged_ms": child.mergedAt, "role": child.role,
            "witness_keys": child.witnessKeys, "depends_on": child.dependsOn}


def witness_json(witness, result):
    return {"key": witness.key, "criterion": witness.criterion,
            "verdict": None if result is None else result.verdict,
            "main_sha": None if result is None else result.mainSha,
            "at": None if result is None else result.at}


def decision_json(decision):
    return {"id": decision.id, "kind": decision.kind,
            "ticket": decision.ticket, "question": decision.question,
            "options": list(decision.options),
            "default_option": decision.defaultOption,
            "answer": decision.answer, "answered_by": decision.answeredBy,
            "answered_ms": decision.answeredAt, "at": decision.at}
