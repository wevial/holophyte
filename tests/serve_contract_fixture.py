"""Seed and normalize the JSON contracts shared with the console tests."""
import json
import re

import holophyte.serve.serve_findings
import holophyte.serve.serve_history
import holophyte.serve.serve_runs
import holophyte.serve.serve_stories
import holophyte.serve.server
import holophyte.serve.views
import store
from holophyte.config.project import Project
from store.follow_ups import record_follow_up
from store.operator_notes import consume
from store.steer_notes import steer
from store.stories import (
    answer_decision,
    approve_story,
    file_story,
    park_story,
    record_witness_result,
)
from tests.phase_fixture import park_run

NOW = 1_750_000_000_000
MIGRATED_BY = re.compile(r" by \S+, pid \d+, [^)]*\)$")
STORY_WITNESS = {"key": "W1", "criterion": "the story works",
                 "file": "tests/test_story.py",
                 "command": "python3 -m unittest tests.test_story",
                 "source": "def test_it_works():\n    pass\n"}


def normalize_contract(value, key=""):
    """Keep nulls and shapes; replace volatile clocks/IDs/hosts/paths by type."""
    if isinstance(value, dict):
        return {k: normalize_contract(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_contract(v) for v in value]
    if value is None:
        return None
    if key in {"host", "project"}:
        return "writer" if key == "host" else "/repo"
    if key in {"id", "event_id", "run_id", "pid", "now", "at", "started_ms",
               "ended_ms", "work_started_ms", "verify_started_ms",
               "approved_at"}:
        return 1 if key in {"id", "event_id", "run_id", "pid"} else NOW
    if key == "text":
        return MIGRATED_BY.sub(" by BUILD, pid 1, COMMAND)", value)
    return value


def contract_answers(case):
    """A reviewed run with a pending follow-up, an unestimated run predating
    host recording with one posted note, an approved pull request waiting
    for a claim, one waiting on review, and an unspecced story over the
    unestimated and approved tickets."""
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
        record_follow_up(conn, case.run, "c" * 40, "feature", True,
                         "Validate input at the API edge", "validate-edge",
                         path="app.py", line=7, now=NOW - 5_000)
        store.record_event(conn, case.run, "criteria",
                           "review round 1: 0 of 1 criteria met",
                           level="detail", payload=json.dumps(dict(round=1, criteria=[
                               dict(n=1, status="not_met", note="input unvalidated",
                                    text="Given input, then it is validated")])),
                           now=NOW)
        project = store.tickets.ensure_project(conn, "team-1", case.target)
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-8", linear_identifier="KO-8",
            title="Unestimated ticket", acceptance_criteria=["Run without an estimate"],
            verification_commands=["echo ok"], time_box_ms=None)
        store.tickets.transition(conn, ticket, "in_flight")
        steer(conn, ticket, "Keep the estimate", "operator", now=NOW - 1)
        unestimated_run = store.claim(conn, project, ticket, now=NOW)
        store.mark_note_posted(conn, store.record_note(
            conn, ticket, "escalation", "Needs an estimate", "estimate",
            run_id=unestimated_run, now=NOW), now=NOW)
        approved = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-9", linear_identifier="KO-9",
            title="Approved ticket", acceptance_criteria=["Merge once approved"],
            verification_commands=["echo ok"], time_box_ms=None)
        store.tickets.transition(conn, approved, "in_flight")
        parked = store.claim(conn, project, approved, now=NOW - 180_000)
        store.tickets.transition(conn, approved, "blocked_on_operator")
        park_run(conn, parked, "awaiting_merge_approval", "PR open",
                 candidate_sha="a" * 40, pr_url="https://github.com/example/repo/pull/9",
                 park_kind="pull_request", now=NOW - 120_000)
        store.approve(conn, approved, "go", now=NOW - 60_000)
        waiting = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-10", linear_identifier="KO-10",
            title="Reviewed ticket", acceptance_criteria=["Merge once reviewed"],
            verification_commands=["echo ok"], time_box_ms=None)
        store.tickets.transition(conn, waiting, "in_flight")
        reviewed = store.claim(conn, project, waiting, now=NOW - 90_000)
        store.tickets.transition(conn, waiting, "blocked_on_operator")
        park_run(conn, reviewed, "awaiting_merge_approval", "PR open\nAwaiting review",
                 candidate_sha="b" * 40, pr_url="https://github.com/example/repo/pull/10",
                 park_kind="pull_request", now=NOW - 30_000)
        story = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-20", linear_identifier="KO-20",
            title="Story over KO-8 and KO-9")
        file_story(conn, story, [STORY_WITNESS],
                   [(ticket, "advances", ("W1",)), (approved, "scaffolding", ())])
        conn.execute(
            "UPDATE runs SET workingMs = NULL, verifyMs = NULL WHERE id = ?",
            (unestimated_run,))
        conn.commit()
    case.null_host(unestimated_run)
    target = Project.locate(case.target)
    answers = {}
    for name, (code, body) in {
        "status": holophyte.serve.server.status(target, now=NOW, started_ms=NOW),
        "attention": holophyte.serve.server.attention(target, now=NOW),
        "run-detail": holophyte.serve.serve_runs.run_detail(
            target, str(case.run), now=NOW
        ),
        "run-detail-unestimated": holophyte.serve.serve_runs.run_detail(
            target, str(unestimated_run), now=NOW
        ),
        "shipped": holophyte.serve.serve_runs.shipped(target, "outcome=all"),
        "ledger": holophyte.serve.serve_runs.ledger(target, "since=0"),
        "history": holophyte.serve.serve_history.history(
            target, f"since={NOW - 100_000}", now=NOW),
        "findings": holophyte.serve.serve_findings.findings(target, "since=0"),
        "board": holophyte.serve.views.board(target, now=NOW),
        "ticket": holophyte.serve.views.ticket_detail(target, "KO-8"),
    }.items():
        case.assertEqual(code, 200)
        answers[name] = normalize_contract(body)
    with store.open(str(case.db)) as conn:
        store.record_agent_session(conn, case.run, "session-1", "implement",
                                   "primary")
        for role, route, label, seconds in [
                ("implement", "primary", "claude opus", 372.5),
                ("review", "fallback", "codex gpt-5.6-sol", 95.25)]:
            store.record_event(conn, case.run, "agent_turn", f"{role} turn ended",
                               level="detail", payload=json.dumps(dict(
                                   role=role, label=label, route=route,
                                   exit_status=0, timed_out=False,
                                   seconds=seconds)))
    code, body = holophyte.serve.serve_runs.run_turns(target, str(case.run))
    case.assertEqual(code, 200)
    answers["run-turns"] = normalize_contract(body)
    seed_story(case.db, case.target)
    code, body = holophyte.serve.serve_stories.story_detail(target, "KO-11")
    case.assertEqual(code, 200)
    answers["story"] = normalize_contract(body)
    return answers


def seed_story(db, repo):
    """KO-11's story over KO-12 and KO-13, which depends on KO-12: its
    witness recorded green and one decision answered."""
    with store.open(str(db)) as conn:
        project = store.tickets.ensure_project(conn, "team-1", repo)
        parent, first, second = (store.tickets.mirror_ticket(
            conn, project, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=title,
            acceptance_criteria=[criterion], verification_commands=["echo ok"],
            depends_on=depends, now=NOW) for n, title, criterion, depends in (
                (11, "Export recovers", "Exports survive a restart", None),
                (12, "Checkpoint exports", "Exports checkpoint", None),
                (13, "Resume exports", "Exports resume", ["issue-12"])))
        file_story(conn, parent, [dict(
            key="W1", criterion="Exports survive a restart",
            file="tests/test_export.py", command="python3 -m unittest",
            source="assert resumed")], [(first, "advances", ("W1",)),
                                        (second, "completes", ("W1",))])
        (revision,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                                   (parent,)).fetchone()
        approve_story(conn, parent, revision, "operator", "Plan approved",
                      now=NOW)
        record_witness_result(conn, parent, "W1", "c" * 40, "green",
                              "baseline", now=NOW)
        decision = park_story(conn, parent, "unmet", "Replan the export?",
                              ["replan", "abandon"], "replan",
                              ticket_id=second, now=NOW)
        answer_decision(conn, decision, "replan", "operator",
                        "Replan around the checkpoint", now=NOW)
