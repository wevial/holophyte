"""The public surface of the `store` package, held to explicit allow-lists.

Every public function is porting work for the Rust replacement. Additions
and removals must be deliberate: these lists hold the package, schema,
read and working-clock surfaces. Operator names are also read from
AGENTS.md so the protocol and module cannot drift.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.operate
import store.read
import store.schema
import store.tickets
import store.working

# Alphabetical. Edit this list in the same change that adds or removes a
# public function, and say why in the commit.
EXPECTED = [
    # KO-592: `--abort`, an emergency stop recorded before the run is marked.
    "abort",
    # KO-258: the operator's `--approve`, the release of a run parked in
    # `awaiting_merge_approval`, one transaction like `requeue`.
    "approve",
    # KO-262: `--babysit`, the release of a run parked on its pull request
    # back to the babysitter; `approve`'s transaction with its own action
    # (`shepherd` until KO-374 renamed it with the rows).
    "babysit",
    "claim",
    "clear_merge_sha",  # KO-582: named writers replace loop SQL.
    "clear_push",  # KO-740: a store-mode push landed or dropped.
    "contract_drift",
    "contract_snapshot",
    "ensure_project",
    "register_project",  # KO-586: explicit project registration and admission.
    "list_projects",
    "set_admission",
    "findings_fingerprint",
    "findings_overlap",
    "heartbeat",
    "hold",  # KO-578: project admission operator verbs.
    "init",
    "latest_supervisor_heartbeat",
    # KO-747: a store-mode note's post, accepted or failed, by the host sweep.
    "mark_note_failed",
    "mark_note_posted",
    "mirror_ticket",
    "open",
    # KO-256: `[merge] approve = "human"` parks a live run in
    # `awaiting_merge_approval` and frees its lease without ending it.
    "park",
    "pickable",
    # KO-343: the scheduler's one-read count of the claimable queue.
    "pickable_tickets",
    # KO-736: the mirror's revision of a ticket's board-owned fields.
    "record_board_fields",
    "record_agent_session",  # KO-569: latest session and ordered event history.
    "record_event",
    "record_intervention",
    # KO-250: the run's narrative lives in the store; `board.ledger()` writes
    # the row here before it posts the board comment that projects it.
    "record_ledger",
    "record_loop_restart",
    "record_loop_return",
    # KO-742: a ticket's board note, written with its ledger entry in store
    # mode for the host sweep to post.
    "record_note",
    # KO-368: what one read of a parked run's pull request saw, the four
    # `runs.prSeen*` columns in one statement, for `park()` and the loop's
    # reconcile alike.
    "record_pr_seen",
    # KO-740: a store-mode status push, queued for the host sweep.
    "record_push",
    # KO-665: a project-level decision, recorded with no run.
    "record_project_intervention",
    "record_review_round",
    "record_strike",
    "record_supervisor_heartbeat",
    "pause",
    "release",
    "release_hold",
    # KO-665: a foreign key naming a dropped table, rewritten by API with a
    # dry run and a recorded decision instead of by hand in `sqlite_master`.
    "repair_references",
    # KO-297: the operator's `--repoint`, a parked candidate moved to a
    # rebuilt branch tip as a recorded intervention instead of raw SQL.
    "repoint",
    # KO-223: the operator's requeue-after-failure, one transaction behind
    # `--requeue`, so the ladder's rung-3 pair is a rung-1 command.
    "requeue",
    # KO-365: the merge gate's conflict reason, recognised by `requeue()`
    # and the loop's `--requeue` candidate read alike.
    "is_gate_conflict",
    "render_state_graph",
    "render_state_graph_section",
    "resume",
    "run_contract",
    "run_phase",
    "set_board_state",
    "set_branch",
    "set_gone_since",  # KO-739: the store-mode sweep's gone stamp.
    "set_outcome_reason",
    "set_phase",
    "set_pull_request",
    "set_question",
    # KO-321: the review-round cap the loop gave a run, written where the
    # loop computes it so `/runs/N` serves the cap this run had.
    "set_review_round_cap",
    "stamp_board_ask",
    "transaction",
    "transition",
    "unreturned_loop_restarts",
    "walk_ticket",
]

# Alphabetical, same rule, for the classes the `store` package exposes:
# the exceptions a caller matches on are surface too, each an error
# variant the Rust port carries — two re-exported from `store/tickets.py`.
EXPECTED_CLASSES = [
    "ApproveRefused",
    "ClaimConflict",
    "GuidanceNotAccepted",
    "IllegalTransition",  # Shared ticket-status and run-phase refusal.
    "Pickability",
    "RepointRefused",
    "RequeueRefused",
    "ResumeRefused",
    # Phase 3 stage 3: a store-mode claim's revision moved since admission.
    "RevisionMoved",
    "RunEnded", "SchemaNewer", "SchemaOlder",  # Schema compatibility refusals.
]

# Alphabetical, same rule, for `store/schema.py`: KO-391 moved the schema,
# the migration ladder and the connection there; the package re-exports
# them so `store.open()` still answers.
EXPECTED_SCHEMA = [
    "init",
    # KO-661: the newest migrate note, read by the report header and by
    # open() for the floor an older build may open a newer store from.
    "latest_migration_note",
    "open",
    "transaction",
]

# Alphabetical, same rule. One read per SELECT the factory used to embed;
# a read that fetches the same row with a different column subset is not a
# new function but a wider row type.
EXPECTED_READ = [
    # KO-258: the loop's claim path asks whether the ticket's newest prior
    # run left an approved candidate to take to the merge gate.
    "approved_candidate",
    "last_independent_verdict",
    "babysit_note",  # KO-462: the resumed implementer reads the operator note.
    # KO-245: the `serve` daemon's `/attention` reads.
    "blocked_tickets",
    # Phase 3 stage 3: the store's ready queue.
    "claimable",
    "ended_runs",
    "failed_attempts_since",
    # KO-435: pages of ended runs of any outcome for `/shipped`.
    "finished_runs",
    "latest_human_intervention_at",
    # KO-250: a run's ledger entries, oldest first.
    "ledger",
    # KO-278: the `serve` daemon's `/ledger` window across runs.
    "ledger_since",
    "live_runs",
    # Consolidation stage 1: the host daemon's bound on one store's lock
    # wait, so a locked store is its project's error inside a client's
    # request limit. Not a read; the context `open_readonly()` reads in.
    "lock_wait",
    # KO-274: the `serve` daemon's `/shipped` page of merged runs.
    "merged_runs",
    # KO-269: the `serve` daemon's `/runs/N` reads.
    "narrative_events",
    "newest_ended_rounds",
    # KO-348: the `serve` daemon's anchor for an action's ledger note.
    "newest_run_id",
    # KO-280: the `serve` daemon's `/board` read of the open tickets.
    "open_tickets",
    "open_readonly",
    # KO-747: the store-mode notes the host sweep has yet to post.
    "pending_notes",
    # KO-409: the supervisor's read of the ready tickets owed a loop,
    # however they became ready.
    "ready_tickets",
    "recent_failed_runs",
    "review_rounds",
    "rounds_of",
    "run_detail",
    "run_snapshot",
    "strike",
    # KO-218: the `serve` daemon's supervisor read, so it needs nothing from
    # `store` itself.
    "supervisor_beat",
    "ticket_by_id",
    # KO-328: the `serve` daemon's `/tickets/KO-n` read of one mirrored
    # ticket, body included.
    "ticket_by_identifier",
    # KO-737: a ticket's revisions, newest first, for `/tickets/KO-n`.
    "ticket_revisions",
    # KO-747: a ticket's notes, oldest first, for `/tickets/KO-n`.
    "ticket_notes",
    # KO-705: human interventions per merged run, for `--report` and
    # `/status`.
    "toil_since",
]

AGENTS_MD = Path(__file__).resolve().parent.parent / "AGENTS.md"

def public_functions(module=store):
    """Public function names of `module` without a leading `_`.

    The `store` package is checked by namespace — a name bound on `store`
    counts, so the `open`/`init`/`transaction` re-exports from
    `store.schema` stay surface. Any other module is checked by
    `__module__`, so names it merely imports (`dataclass`, `Path`) do not.
    """
    return sorted(
        name
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_")
        and (module is store or obj.__module__ == module.__name__)
    )


def public_classes(module=store):
    """Names of the classes `module` exposes without a leading `_`."""
    return sorted(
        name for name, obj in inspect.getmembers(module, inspect.isclass)
        if not name.startswith("_")
        and (module is store or obj.__module__ == module.__name__)
    )


def operator_api_names():
    """The backticked names in AGENTS.md's "Operator store API" bullet."""
    text = AGENTS_MD.read_text()
    match = re.search(
        r"\*\*Operator store API, by name\.\*\*(.*?)even when", text, re.S
    )
    assert match, "AGENTS.md no longer has the operator store API bullet"
    return re.findall(r"`([a-z_]+)`", match.group(1))


class StoreSurfaceTests(unittest.TestCase):
    def test_loop_has_no_raw_state_updates(self):
        root = AGENTS_MD.parent
        pattern = re.compile(r"\bUPDATE\s+(?:runs|tickets|projects)\b", re.I)
        matches = [
            f"{path.relative_to(root)}:{source.count(chr(10), 0, match.start()) + 1}"
            for path in sorted((root / "holophyte").rglob("*.py"))
            for source in [path.read_text()]
            for match in pattern.finditer(source)
        ]
        self.assertEqual(matches, [], "raw store writes: " + ", ".join(matches))

    def test_enum_refactor_preserves_exported_names_and_values(self):
        from tests.store_enums_fixture import BASELINE

        for name, expected in BASELINE.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(store, name), expected)

    def test_public_functions_match_the_allow_list(self):
        for module, expected in ((store, EXPECTED),
                                 (store.schema, EXPECTED_SCHEMA),
                                 # KO-457: persisted work boundaries and live read.
                                 (store.working, ["agent_work", "effective_work",
                                                  "settle_work", "verify_work",
                                                  "working"])):
            actual = public_functions(module)
            unexpected = sorted(set(actual) - set(expected))
            missing = sorted(set(expected) - set(actual))
            self.assertEqual(
                (unexpected, missing), ([], []),
                f"{module.__name__} public surface drifted: not in"
                f" allow-list {unexpected}, in allow-list but gone"
                f" {missing}; update the allow-list in"
                f" tests/test_store_surface.py deliberately",
            )

    def test_public_classes_match_the_allow_list(self):
        actual = public_classes()
        unexpected = sorted(set(actual) - set(EXPECTED_CLASSES))
        missing = sorted(set(EXPECTED_CLASSES) - set(actual))
        self.assertEqual(
            (unexpected, missing), ([], []),
            f"store public classes drifted: not in allow-list {unexpected},"
            f" in allow-list but gone {missing}; update EXPECTED_CLASSES in"
            f" tests/test_store_surface.py deliberately",
        )

    def test_read_functions_match_the_allow_list(self):
        actual = public_functions(store.read)
        unexpected = sorted(set(actual) - set(EXPECTED_READ))
        missing = sorted(set(EXPECTED_READ) - set(actual))
        self.assertEqual(
            (unexpected, missing), ([], []),
            f"store.read public surface drifted: not in allow-list"
            f" {unexpected}, in allow-list but gone {missing}; update"
            f" EXPECTED_READ in tests/test_store_surface.py deliberately",
        )

    def test_a_read_added_without_the_allow_list_is_named(self):
        """A new public function in `store.read` shows up as `unexpected`.

        Guards the helper's `__module__` filter from both sides: a function
        the module defines is counted, and the names it merely imports
        (`dataclass`, `Path`) are not -- otherwise the allow-list would
        either miss additions or fill up with the standard library.
        """
        def newest_thing(conn):
            return None
        newest_thing.__module__ = store.read.__name__

        with patch.object(store.read, "newest_thing", newest_thing, create=True):
            actual = public_functions(store.read)
        self.assertIn("newest_thing", actual)
        self.assertNotIn("newest_thing", EXPECTED_READ)
        self.assertNotIn("dataclass", public_functions(store.read))

    def test_operator_api_named_in_agents_md_is_present(self):
        names = operator_api_names()
        self.assertEqual(len(names), 8, names)
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, public_functions())
                self.assertIn(name, EXPECTED)


if __name__ == "__main__":
    unittest.main()
