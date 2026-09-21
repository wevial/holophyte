"""Seed and normalize the JSON contracts shared with the console tests."""
import json

import holophyte.serve
import holophyte.serve_runs
import store
from holophyte.target import Target
from store.operator_notes import consume

NOW = 1_750_000_000_000


def normalize_contract(value, key=""):
    """Keep nulls and shapes; replace volatile clocks/IDs/hosts/paths by type."""
    if isinstance(value, dict):
        return {k: normalize_contract(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_contract(v) for v in value]
    if value is None:
        return None
    if key in {"host", "target", "project"}:
        return "writer" if key == "host" else "/repo"
    if key in {"id", "event_id", "run_id", "pid", "now", "at", "started_ms",
               "ended_ms", "work_started_ms", "approved_at"}:
        return 1 if key in {"id", "event_id", "run_id", "pid"} else NOW
    return value


def contract_answers(case):
    """A live reviewed run, including every run-detail collection."""
    case.seed()
    with store.open(str(case.db)) as conn:
        store.record_review_round(
            conn, case.run, 1, "changes_requested", "reviewer",
            started_at=NOW - 20_000, ended_at=NOW - 10_000,
            findings=[
                dict(path="app.py", line=7, severity="p1", criterion=None,
                     message="Validate input", kind="thread", author="review-bot",
                     author_kind="bot", summary="Validate input", verdict="ADDRESS",
                     raw="Please validate input", url="https://example.test/thread"),
                dict(kind="instruction", path="app.py", line=None, author="operator",
                     severity="nit", message="Keep validation",
                     request="Keep validation", url="https://example.test/instruction",
                     outcome="changed", reply="Validation retained"),
            ])
        store.record_event(conn, case.run, "operator_note", "operator: Keep validation",
                           level="detail", payload=json.dumps(
                               dict(note="Keep validation", author="operator")),
                           now=NOW)
        event_id = conn.execute(
            "SELECT id FROM runEvents WHERE kind = 'operator_note'").fetchone()[0]
        consume(conn, case.run, [event_id], 1)
        store.record_event(conn, case.run, "bot_finding", "Review is advisory", now=NOW)
    target = Target.locate(case.target)
    answers = {}
    for name, (code, body) in {
        "status": holophyte.serve.status(target, now=NOW, started_ms=NOW),
        "run-detail": holophyte.serve_runs.run_detail(target, str(case.run), now=NOW),
    }.items():
        case.assertEqual(code, 200)
        answers[name] = normalize_contract(body)
    return answers
