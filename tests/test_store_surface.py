"""The public surface of the `store` package, held to explicit allow-lists.

Every public function is porting work for the Rust replacement, so a new one
has to be a deliberate addition and an orphan has to be a deliberate removal:
both show up here as a failure naming the function. The writers and the state
graph live in `store/__init__.py` (`EXPECTED`); the typed read views live in
`store/read.py` (`EXPECTED_READ`). The operator names in AGENTS.md are read
from that file rather than retyped, so the protocol and the module cannot
drift apart silently.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.read

# Alphabetical. Edit this list in the same change that adds or removes a
# public function, and say why in the commit.
EXPECTED = [
    # KO-258: the operator's `--approve`, the release of a run parked in
    # `awaiting_merge_approval`, one transaction like `requeue`.
    "approve",
    "claim",
    "contract_drift",
    "contract_snapshot",
    "ensure_project",
    "findings_fingerprint",
    "findings_overlap",
    "heartbeat",
    "init",
    "latest_supervisor_heartbeat",
    "mirror_ticket",
    "open",
    # KO-256: `[merge] approve = "human"` parks a live run in
    # `awaiting_merge_approval` and frees its lease without ending it.
    "park",
    "pickable",
    "record_event",
    "record_intervention",
    "record_loop_restart",
    "record_loop_return",
    "record_review_round",
    "record_strike",
    "record_supervisor_heartbeat",
    "release",
    # KO-223: the operator's requeue-after-failure, one transaction behind
    # `--requeue`, so the ladder's rung-3 pair is a rung-1 command.
    "requeue",
    "render_state_graph",
    "render_state_graph_section",
    "resume",
    "run_contract",
    "run_phase",
    "set_phase",
    "transaction",
    "transition",
    "unreturned_loop_restarts",
    "walk_ticket",
]

# Alphabetical, same rule, for the classes `store/__init__.py` defines: the
# exceptions a caller matches on are surface too, and each is an error
# variant the Rust port has to carry.
EXPECTED_CLASSES = [
    "ApproveRefused",
    "ClaimConflict",
    "GuidanceNotAccepted",
    "IllegalTransition",
    "Pickability",
    "RequeueRefused",
    "ResumeRefused",
    "RunEnded",
]

# Alphabetical, same rule. One read per SELECT the factory used to embed;
# a read that fetches the same row with a different column subset is not a
# new function but a wider row type.
EXPECTED_READ = [
    # KO-258: the loop's claim path asks whether the ticket's newest prior
    # run left an approved candidate to take to the merge gate.
    "approved_candidate",
    # KO-245: the `serve` daemon's `/attention` reads.
    "blocked_tickets",
    "ended_runs",
    "failed_attempts_since",
    "latest_human_intervention_at",
    "live_runs",
    "newest_ended_rounds",
    "open_readonly",
    "recent_failed_runs",
    "review_rounds",
    "run_snapshot",
    "strike",
    # KO-218: the `serve` daemon's supervisor read, so it needs nothing from
    # `store` itself.
    "supervisor_beat",
    "ticket_by_id",
]

AGENTS_MD = Path(__file__).resolve().parent.parent / "AGENTS.md"


def public_functions(module=store):
    """Names of the functions `module` itself defines without a leading `_`."""
    return sorted(
        name
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if obj.__module__ == module.__name__ and not name.startswith("_")
    )


def public_classes(module=store):
    """Names of the classes `module` itself defines without a leading `_`."""
    return sorted(
        name
        for name, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == module.__name__ and not name.startswith("_")
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
    def test_public_functions_match_the_allow_list(self):
        actual = public_functions()
        unexpected = sorted(set(actual) - set(EXPECTED))
        missing = sorted(set(EXPECTED) - set(actual))
        self.assertEqual(
            (unexpected, missing), ([], []),
            f"store public surface drifted: not in allow-list {unexpected},"
            f" in allow-list but gone {missing}; update EXPECTED in"
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
        self.assertEqual(len(names), 6, names)
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, public_functions())
                self.assertIn(name, EXPECTED)


if __name__ == "__main__":
    unittest.main()
