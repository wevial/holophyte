"""The public surface of `store.py`, held to an explicit allow-list.

Every public function is porting work for the Rust replacement, so a new one
has to be a deliberate addition and an orphan has to be a deliberate removal:
both show up here as a failure naming the function. The operator names in
AGENTS.md are read from that file rather than retyped, so the protocol and
the module cannot drift apart silently.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path

import store

# Alphabetical. Edit this list in the same change that adds or removes a
# public function, and say why in the commit.
EXPECTED = [
    "claim",
    "contract_drift",
    "contract_snapshot",
    "ensure_project",
    "findings_fingerprint",
    "findings_overlap",
    "init",
    "latest_supervisor_heartbeat",
    "mirror_ticket",
    "open",
    "pickable",
    "record_event",
    "record_intervention",
    "record_loop_restart",
    "record_loop_return",
    "record_review_round",
    "record_strike",
    "record_supervisor_heartbeat",
    "release",
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

AGENTS_MD = Path(__file__).resolve().parent.parent / "AGENTS.md"


def public_functions():
    """Names of the functions `store.py` itself defines without a leading `_`."""
    return sorted(
        name
        for name, obj in inspect.getmembers(store, inspect.isfunction)
        if obj.__module__ == store.__name__ and not name.startswith("_")
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
            f"store.py public surface drifted: not in allow-list {unexpected},"
            f" in allow-list but gone {missing}; update EXPECTED in"
            f" tests/test_store_surface.py deliberately",
        )

    def test_operator_api_named_in_agents_md_is_present(self):
        names = operator_api_names()
        self.assertEqual(len(names), 5, names)
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, public_functions())
                self.assertIn(name, EXPECTED)


if __name__ == "__main__":
    unittest.main()
