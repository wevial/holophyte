"""README's state-machine diagrams are rendered from the code, not drawn.

`store.render_state_graph()` turns a `{state: {next, ...}}` table into a
Mermaid `stateDiagram-v2` block, and README embeds one for the ticket status
table and one for the run phase table between marker comments. The tests
here hold the embedded text to what the live tables render, so a diagram
edited by hand — or a table edited without regenerating — fails by name.

The edge oracle for the ticket graph is the same hand transcription
`test_store_status.py` uses, restated rather than imported so this file does
not pass merely because the module agrees with itself.

Run: python3 -m unittest discover -s tests -p 'test_store_status*' -v
"""
from __future__ import annotations

import re
import unittest
import unittest.mock
from pathlib import Path

import store

README = Path(__file__).resolve().parent.parent / "docs" / "loop.md"

# The §3 drawing plus KO-140's escalation edge, as (from, to) pairs.
TICKET_EDGES = {
    ("needs_spec", "ready"),
    ("ready", "in_flight"),
    ("ready", "blocked_on_deps"),
    ("in_flight", "merged"),
    ("in_flight", "abandoned"),
    ("in_flight", "blocked_on_operator"),
    ("blocked_on_deps", "ready"),
    ("blocked_on_deps", "blocked_on_operator"),
    ("blocked_on_operator", "blocked_on_deps"),
}

EDGE = re.compile(r"^\s*(\S+) --> (\S+)$", re.MULTILINE)


def readme_sections(text):
    """`{marker name: section text}` for every marked section in `text`,
    markers included, exactly as `render_state_graph_section()` writes it."""
    return {name: match.group(0) for name, _ in store.STATE_GRAPHS
            for match in [re.search(
                rf"<!-- {re.escape(name)} -->\n.*?<!-- end {re.escape(name)} -->\n",
                text, re.DOTALL)] if match}


def stale_sections(text):
    """Names of the marked sections in `text` that do not match the code."""
    found = readme_sections(text)
    return [name for name, table in store.STATE_GRAPHS
            if found.get(name)
            != store.render_state_graph_section(name, getattr(store, table))]


class RendererTests(unittest.TestCase):
    def test_ticket_graph_draws_each_legal_edge_once_and_nothing_else(self):
        text = store.render_state_graph(store.TICKET_TRANSITIONS)
        edges = EDGE.findall(text)
        self.assertEqual(len(edges), len(set(edges)), "an edge is drawn twice")
        self.assertEqual(set(edges), TICKET_EDGES)
        self.assertTrue(text.startswith("stateDiagram-v2\n"))

    def test_rendering_is_independent_of_table_order(self):
        forward = {"a": {"c", "b"}, "b": {"a"}, "c": set()}
        backward = {"c": set(), "b": {"a"}, "a": {"b", "c"}}
        self.assertEqual(store.render_state_graph(forward),
                         store.render_state_graph(backward))
        self.assertEqual(
            store.render_state_graph(forward),
            "stateDiagram-v2\n    a\n    b\n    c\n"
            "    a --> b\n    a --> c\n    b --> a\n")


class ReadmeTests(unittest.TestCase):
    def test_the_shipped_readme_matches_the_code(self):
        text = README.read_text()
        self.assertEqual(sorted(readme_sections(text)),
                         sorted(name for name, _ in store.STATE_GRAPHS),
                         "README is missing a marked state-graph section")
        self.assertEqual(stale_sections(text), [],
                         "README state graph differs from the code;"
                         " regenerate with: python3 store.py --state-graph")

    def test_a_hand_added_edge_fails_naming_the_section(self):
        text = README.read_text()
        section = readme_sections(text)["state-graph: tickets"]
        edited = section.replace("    needs_spec --> ready\n",
                                 "    needs_spec --> ready\n"
                                 "    merged --> ready\n")
        self.assertNotEqual(edited, section)
        self.assertEqual(stale_sections(text.replace(section, edited)),
                         ["state-graph: tickets"])

    def test_readme_check_fails_on_the_shipped_readme_when_a_table_changes(self):
        """The guard in the other direction: a table edited without
        regenerating README is what the test exists to catch."""
        text = README.read_text()
        with unittest.mock.patch.dict(
                store.RUN_PHASE_TRANSITIONS,
                {"squashing": frozenset({"done"})}):
            self.assertEqual(stale_sections(text), ["state-graph: runs"])


if __name__ == "__main__":
    unittest.main()
