"""`--status`'s story lines: each open story's children, witnesses and age."""
import collections

from holophyte.config_tables import story_config
from store.stories import (
    CLOSED_STATUSES,
    story,
    story_frontier,
    witness_ledger,
)

SHOWN_STATES = ("planned", "approved", "parked")
CHILD_KINDS = ("merged", "running", "frontier", "waiting")


def story_facts(target, conn, now):
    ids = [row[0] for row in conn.execute(
        "SELECT ticketId FROM stories WHERE state IN (?, ?, ?)"
        " ORDER BY ticketId", SHOWN_STATES)]
    if not ids:
        return []
    max_parallel = story_config(target).max_parallel
    return [_facts(conn, story_id, max_parallel, now) for story_id in ids]


def _facts(conn, story_id, max_parallel, now):
    found = story(conn, story_id)
    identifier, title, filed_at = conn.execute(
        "SELECT linearIdentifier, title, filedAt FROM tickets WHERE id = ?",
        (story_id,)).fetchone()
    ledger = witness_ledger(conn, story_id)
    commit = ledger[-1].mainSha if ledger else None
    verdicts = {row.witnessKey: row.verdict
                for row in witness_ledger(conn, story_id, commit)
                } if commit else {}
    witnesses = [{"key": witness.key, "verdict": verdicts.get(witness.key)}
                 for witness in found.witnesses]
    return {"ticket": identifier, "title": title, "state": found.state,
            "generation": found.generation,
            "children": _children(conn, story_id, max_parallel),
            "witnesses": witnesses, "commit": commit,
            "errors": sum(witness["verdict"] == "error"
                          for witness in witnesses),
            "decisions": len(found.decisions),
            "age_s": None if filed_at is None
            else max(0, (now - filed_at) // 1000)}


def _children(conn, story_id, max_parallel):
    frontier = set(story_frontier(conn, story_id, max_parallel))
    kinds = collections.Counter()
    for identifier, status, live in conn.execute(
            "SELECT t.linearIdentifier, t.status, EXISTS (SELECT 1 FROM runs r"
            " WHERE r.ticketId = t.id AND r.endedAt IS NULL) FROM tickets t"
            " WHERE t.id IN (SELECT ticketId FROM storyChildren"
            " WHERE storyId = ?)", (story_id,)):
        kinds["total"] += 1
        if status in CLOSED_STATUSES:
            kinds[status] += 1
        elif live:
            kinds["running"] += 1
        else:
            kinds["frontier" if identifier in frontier else "waiting"] += 1
    return {kind: kinds[kind] for kind in ("total", *CHILD_KINDS)}


def _age(seconds):
    if seconds is None:
        return "unknown"
    days, hours = divmod(seconds // 3600, 24)
    return f"{days}d {hours}h"


def story_lines(stories):
    lines = []
    for fact in stories:
        children = fact["children"]
        lines.append(
            f"{fact['ticket']} story \"{fact['title']}\"  {fact['state']}"
            f" gen {fact['generation']}  children {children['merged']}/"
            f"{children['total']} merged, {children['running']} running,"
            f" {children['frontier']} frontier, {children['waiting']} waiting")
        witnesses = "  ".join(f"{witness['key']} {witness['verdict'] or 'unrun'}"
                              for witness in fact["witnesses"]) or "none"
        lines.append(
            f"  witnesses {witnesses} @{(fact['commit'] or 'none')[:7]}"
            f"  errors {fact['errors']}  decisions {fact['decisions']}"
            f"  age {_age(fact['age_s'])}")
    return lines
