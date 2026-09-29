"""A story child's ticket text carries the story's standing orders, its merged
dependencies' changed files and the approved source of the witness it
completes; a ticket in no story reads as it always did.

Run: python3 -m unittest discover -s tests -p 'test_story_brief.py' -v
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.loop  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.stories  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.story.story_claim import (  # noqa: E402
    STANDING_ORDERS,
    UPSTREAM,
    WITNESS,
    story_brief,
)
from provider import board_for  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
W2_SOURCE = ("import unittest\n\n\nclass W2(unittest.TestCase):\n"
             "    def test_export(self):\n"
             "        self.assertEqual('```', '`' * 3)\n")
WITNESSES = [
    {"key": "W1", "criterion": "orders export", "file": "tests/test_w1.py",
     "command": "python3 -m unittest tests.test_w1", "source": "assert 1\n"},
    {"key": "W2", "criterion": "the CSV has a header",
     "file": "tests/test_w2.py", "command": "python3 -m unittest tests.test_w2",
     "source": W2_SOURCE}]
ORDERS = ("Keep every CSV column in snake_case.",
          "Never read the orders table outside `export.py`.")


def body(title, depends_on=()):
    return VALID_BODY.replace("# Add a thing", f"# {title}").replace(
        "Depends on: none", f"Depends on: {', '.join(depends_on) or 'none'}")


class StoryBriefTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", self.no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(NATIVE)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)

    def no_linear(self, *args, **kwargs):
        raise AssertionError("a native project asked Linear")

    def file(self, title, column="ready", depends_on=()):
        identifier = store.board.file_ticket(
            self.conn, self.project_id, "NAT", body(title, depends_on),
            column=column)
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def approved_story(self, children, standing_orders=ORDERS,
                       witnesses=WITNESSES):
        parent = self.file("The export story", column="backlog")
        store.stories.file_story(self.conn, parent, witnesses, children,
                                 standing_orders)
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        store.stories.approve_story(self.conn, parent, revision, "operator",
                                    "the plan holds")
        return parent

    def land(self, ticket_id, names, record_sha=True):
        """Merge a branch changing `names` with --no-ff and end the ticket's
        run merged, answering the merge commit."""
        self.git("checkout", "-q", "-b", f"work-{ticket_id}")
        for name in names:
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{name}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"work {ticket_id}")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", f"merge {ticket_id}",
                 f"work-{ticket_id}")
        sha = self.git("rev-parse", "main").strip()
        store.tickets.transition(self.conn, ticket_id, "in_flight")
        run_id = store.claim(self.conn, self.project_id, ticket_id)
        for phase in ("merge_gate", "merging"):
            store.set_phase(self.conn, run_id, phase)
        store.release(self.conn, run_id, "merged",
                      merge_sha=sha if record_sha else None)
        store.tickets.transition(self.conn, ticket_id, "merged")
        return sha

    def section(self, text, heading):
        self.assertIn(heading, text)
        return text.split(heading, 1)[1].split("\n## ", 1)[0]

    def test_the_brief_lists_each_merged_dependency_and_its_changed_files(self):
        a = self.file("Add the exporter")
        b = self.file("Add the CSV writer")
        c = self.file("Wire the export", depends_on=("NAT-1", "NAT-2"))
        self.approved_story([(a, "scaffolding", ()), (b, "advances", ("W1",)),
                             (c, "completes", ("W1", "W2"))])
        a_sha = self.land(a, ["src/exporter.py", "tests/test_exporter.py"])
        b_sha = self.land(b, ["src/csv_writer.py"])

        upstream = self.section(story_brief(self.project, self.conn, c),
                                UPSTREAM)

        self.assertIn(f"- NAT-1 Add the exporter: merge commit {a_sha}",
                      upstream)
        self.assertIn(f"- NAT-2 Add the CSV writer: merge commit {b_sha}",
                      upstream)
        a_part = upstream.split("NAT-1", 1)[1].split("NAT-2", 1)[0]
        b_part = upstream.split("NAT-2", 1)[1]
        self.assertIn("src/exporter.py", a_part)
        self.assertIn("tests/test_exporter.py", a_part)
        self.assertNotIn("src/csv_writer.py", a_part)
        self.assertIn("src/csv_writer.py", b_part)
        self.assertNotIn("README.md", upstream)

    def test_the_brief_holds_the_standing_orders_and_the_completed_witness(self):
        a = self.file("Add the exporter")
        d = self.file("Write the header", depends_on=("NAT-1",))
        self.approved_story([(a, "completes", ("W1",)),
                             (d, "completes", ("W2",))])
        self.land(a, ["src/exporter.py"], record_sha=False)

        brief = story_brief(self.project, self.conn, d)

        orders = self.section(brief, STANDING_ORDERS)
        self.assertEqual([line for line in orders.splitlines() if line],
                         [f"- {order}" for order in ORDERS])
        witness = self.section(brief, WITNESS)
        self.assertIn("`tests/test_w2.py`", witness)
        self.assertIn(W2_SOURCE, witness)
        self.assertNotIn("assert 1", brief)
        self.assertIn("- NAT-1 Add the exporter: merge commit not recorded",
                      self.section(brief, UPSTREAM))

    def test_a_source_without_a_final_newline_reads_apart_from_one_with(self):
        d = self.file("Write the header")
        witnesses = [dict(WITNESSES[0], source="assert True"),
                     dict(WITNESSES[1], source="assert True\n")]
        self.approved_story([(d, "completes", ("W1", "W2"))],
                            witnesses=witnesses)

        brief = story_brief(self.project, self.conn, d)

        bare, ended = brief.split(WITNESS)[1:]
        self.assertIn("without a final newline", bare)
        self.assertNotIn("without a final newline", ended)
        self.assertIn("with a final newline", ended)
        self.assertNotEqual(bare.replace("W1: orders export", ""),
                            ended.replace("W2: the CSV has a header", ""))

    def test_a_dependency_changing_300_files_is_capped_at_2048_bytes(self):
        a = self.file("Generate the fixtures")
        c = self.file("Use the fixtures", depends_on=("NAT-1",))
        self.approved_story([(a, "advances", ("W1",)),
                             (c, "completes", ("W1", "W2"))])
        self.land(a, [f"fixtures/order_{n:03d}.csv" for n in range(300)])

        brief = story_brief(self.project, self.conn, c)

        upstream = UPSTREAM + self.section(brief, UPSTREAM)
        self.assertLessEqual(len(upstream.rstrip("\n").encode()), 2048)
        listed = upstream.count("fixtures/order_")
        self.assertGreater(listed, 0)
        self.assertIn(f"({300 - listed} changed files left out", upstream)

    def test_the_loop_gives_a_childs_turns_the_brief_and_a_plain_ticket_none(self):
        a = self.file("Add the exporter")
        b = self.file("Add the CSV writer")
        c = self.file("Wire the export", depends_on=("NAT-1", "NAT-2"))
        self.file("Fix the unrelated typo")
        self.approved_story([(a, "scaffolding", ()), (b, "advances", ("W1",)),
                             (c, "completes", ("W1", "W2"))])
        fake = FakeAgent(*[step for _ in range(4)
                           for step in (Commit("work"), APPROVE)])
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop.loop, "agent", fake), \
                patch("holophyte.review.freshness.critic_admits",
                      return_value=True):
            holophyte.cli.operator.main(self.project, self.board)

        self.assertEqual(self.read(
            "SELECT linearIdentifier, status FROM tickets WHERE id NOT IN"
            " (SELECT ticketId FROM stories) ORDER BY id"),
            [("NAT-1", "merged"), ("NAT-2", "merged"), ("NAT-3", "merged"),
             ("NAT-4", "merged")], out.getvalue())
        child = [t for t in fake.turns if "# Wire the export\n" in t.goal]
        plain = [t for t in fake.turns if "# Fix the unrelated typo\n" in t.goal]
        self.assertEqual([t.role for t in child], ["implement", "review"])
        self.assertEqual([t.role for t in plain], ["implement", "review"])
        for turn in child:
            self.assertIn(UPSTREAM, turn.goal)
            upstream = turn.goal.split(UPSTREAM, 1)[1]
            self.assertIn("- NAT-1 Add the exporter: merge commit", upstream)
            self.assertIn("- NAT-2 Add the CSV writer: merge commit", upstream)
        for turn in plain:
            for heading in (STANDING_ORDERS, UPSTREAM, WITNESS, "## Story"):
                self.assertNotIn(heading, turn.goal)

    def test_a_dependency_too_long_to_list_is_still_counted(self):
        a = self.file("Generate " + "fixtures " * 211)
        c = self.file("Use the fixtures", depends_on=("NAT-1",))
        self.approved_story([(a, "advances", ("W1",)),
                             (c, "completes", ("W1", "W2"))])
        self.land(a, [f"fixtures/order_{n:03d}.csv" for n in range(300)])

        upstream = UPSTREAM + self.section(
            story_brief(self.project, self.conn, c), UPSTREAM)

        self.assertLessEqual(len(upstream.rstrip("\n").encode()), 2048)
        self.assertIn("(300 changed files and 1 dependencies left out",
                      upstream)
