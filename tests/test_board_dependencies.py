"""KO-743: a store-mode queue mirror imports blocked issues with their blockers.

The ready listing drops every issue an open `blocks` relation blocks, so
the store never saw a blocked ticket and nothing wrote `dependsOn`. The
boards now answer `listing()` -- the Ready column, blocked issues
included, each carrying `blocked_by`, its open blockers' board ids -- and
a store-mode `_mirror_queue()` mirrors it, writing `dependsOn` from
`blocked_by` on every pass and walking a blocked `ready` row to
`blocked_on_deps` and back once its blockers are gone. Mirror mode lists
and mirrors exactly as before.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402
from test_provider import FakeLinear  # noqa: E402

import holophyte.dispatch  # noqa: E402
import holophyte.runs  # noqa: E402
import linear_provider  # noqa: E402
import provider as board_seam  # noqa: E402
import store.tickets as tickets  # noqa: E402


def blocks(linear, blocker, blocked):
    """`blocker` blocks `blocked`, as Linear lists it: on the blocker."""
    linear.issues[blocker]["relations"]["nodes"].append(
        {"type": "blocks", "relatedIssue": {"identifier": blocked}})


class BoardDependencyTests(LoopFixture):
    """Open KO-1 blocks KO-2 and Done KO-4 blocks KO-3; KO-1..3 in Todo."""

    def setUp(self):
        super().setUp()
        self.linear = FakeLinear()
        for n in (1, 2, 3):
            self.linear.add(f"KO-{n}", "a thing", VALID_BODY)
        self.linear.add("KO-4", "done thing", VALID_BODY, state="Done")
        blocks(self.linear, "KO-1", "KO-2")
        blocks(self.linear, "KO-4", "KO-3")
        patcher = patch.object(linear_provider, "_gql", self.linear.gql)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mirror(self, store_mode):
        """One queue-mirror pass over the board; the listing it returned."""
        board = board_seam.LinearBoard("proj", "team", store_mode=store_mode)
        conn = holophyte.runs.open_store(self.project)
        self.addCleanup(conn.close)
        project_id = tickets.ensure_project(conn, board.team, self.target)
        with patch.object(sys, "stdout", io.StringIO()):
            listed = holophyte.dispatch._mirror_queue(
                self.project, conn, project_id, board)
        conn.commit()
        return [t["id"] for t in listed]

    def rows(self):
        conn = holophyte.runs.open_store(self.project)
        try:
            return {ident: (json.loads(deps), status, column)
                    for ident, deps, status, column in conn.execute(
                        "SELECT linearIdentifier, dependsOn, status, boardColumn"
                        " FROM tickets")}
        finally:
            conn.close()

    def test_listing_keeps_the_blocked_issue_and_names_its_open_blocker(self):
        listed = {t["id"]: t["blocked_by"]
                  for t in linear_provider.listing("proj")}
        self.assertEqual(listed, {"KO-1": [], "KO-2": ["uuid-KO-1"], "KO-3": []})
        self.assertEqual([i["identifier"]
                          for i in linear_provider.list_ready_issues("proj")],
                         ["KO-1", "KO-3"])

    def test_store_mode_mirrors_the_blocker_and_follows_its_removal(self):
        self.assertEqual(self.mirror(store_mode=True), ["KO-1", "KO-3"])
        self.assertEqual(self.rows()["KO-2"],
                         (["uuid-KO-1"], "blocked_on_deps", "ready"))
        self.assertEqual(self.rows()["KO-1"], ([], "ready", "ready"))

        self.linear.issues["KO-1"]["relations"]["nodes"].clear()
        self.assertEqual(self.mirror(store_mode=True), ["KO-1", "KO-2", "KO-3"])
        self.assertEqual(self.rows()["KO-2"], ([], "ready", "ready"))

    def test_mirror_mode_lists_only_the_unblocked_and_keeps_dependencies(self):
        conn = holophyte.runs.open_store(self.project)
        project_id = tickets.ensure_project(conn, "team", self.target)
        tickets.mirror_ticket(conn, project_id, "uuid-KO-3", "KO-3", "a thing",
                              depends_on=["uuid-KO-9"])
        conn.commit()
        conn.close()

        self.assertEqual(self.mirror(store_mode=False), ["KO-1", "KO-3"])
        rows = self.rows()
        self.assertNotIn("KO-2", rows)
        self.assertEqual(rows["KO-3"][0], ["uuid-KO-9"])
        self.assertEqual(rows["KO-1"][0], [])
