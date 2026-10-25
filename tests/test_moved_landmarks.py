"""A stale park whose landmarks main's history renamed is rewritten and re-readied.

Run: python3 -m unittest discover -s tests -p 'test_moved_landmarks.py' -v
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import (  # noqa: E402
    APPROVE,
    Commit,
    FakeAgent,
    Reply,
    no_agent_processes,
)
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import holophyte.review.moved_landmarks  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board import linear_provider  # noqa: E402
from holophyte.board.board_sync import owed  # noqa: E402
from holophyte.board.provider import board_for  # noqa: E402
from holophyte.config.config_tables import sweep_config  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.review.freshness import park_stale  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
OLD = "lotus/agents.py"
NEW = "lotus/features/agents.py"
NEVER = "lotus/never.py"
NAMING_OLD = (VALID_BODY
              .replace("when it runs", f"when `{OLD}` runs")
              .replace("* Everything else.", f"* Anything else in `{OLD}`.")
              .replace("## Implementation notes\n\n* None.\n",
                       f"## Implementation notes\n\n* Extend `{OLD}` with the"
                       f" thing, near `{OLD}:2`; plain {OLD} says so.\n"))
WITH_MODULE = NAMING_OLD.replace(
    "```\necho ok\n```",
    "```\npython3 -m unittest lotus.agents\n"
    "(python3 -m unittest lotus.agents)&& echo ok\n"
    "python3 -m unittest \"lotus.agents\" && echo 'lotus.agents helper'\n"
    "python3 -m unittest lotus'.agents' && echo prefix\\ lotus.agents\n```")
TOOLS = "lotus/tools"
KIT = "lotus/kit"
NAMING_TOOLS = (VALID_BODY
                .replace("when it runs", "when `lotus/tools/stop.py` runs")
                .replace("## Implementation notes\n\n* None.\n",
                         "## Implementation notes\n\n* Extend `lotus/tools`"
                         " (`lotus/tools/`) through `lotus/tools/run.py`.\n"
                         "* Add `lotus/tools/fresh.py` (new).\n"))
NAMING_DIR = VALID_BODY.replace("## Implementation notes\n\n* None.\n",
                                f"## Implementation notes\n\n* Extend `{TOOLS}`.\n")


def no_linear(*args, **kwargs):
    raise AssertionError("a native project asked Linear")


class MovedLandmarkTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(NATIVE)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)
        (self.target / "lotus").mkdir()
        (self.target / OLD).write_text(
            "".join(f"AGENT_{n} = {n}\n" for n in range(40)))
        self.git("add", OLD)
        self.commit("add the agents module")

    def commit(self, message, seconds=None):
        """Commit on main, dated `seconds` since the epoch when given."""
        env = None if seconds is None else {
            **os.environ, "GIT_COMMITTER_DATE": f"@{seconds} +0000",
            "GIT_AUTHOR_DATE": f"@{seconds} +0000"}
        subprocess.run(["git", "commit", "-q", "-m", message],
                       cwd=str(self.target), env=env, check=True,
                       capture_output=True)
        return self.git("rev-parse", "--short", "HEAD").strip()

    def file(self, body):
        self.assertEqual(store.board.file_ticket(
            self.conn, self.project_id, "NAT", body, column="ready"), "NAT-1")
        (at,) = self.read("SELECT at FROM ticketRevisions")[0]
        self.filed = at // 1000

    def move(self, old=OLD, new=NEW, after=60):
        (self.target / new).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", old, new)
        return self.commit(f"move {old}", self.filed + after)

    def park(self, *paths):
        with patch.object(sys, "stdout", io.StringIO()):
            park_stale(self.project, self.conn, self.project_id, self.board,
                       self.board.fetch_task("NAT-1"),
                       [f"`{p}` (named in Implementation notes) is not on main"
                        for p in paths])
        self.assertEqual(self.status(), "needs_spec")

    def sweep(self):
        out = io.StringIO()
        with closing(open_store(self.project)) as conn:
            owed(self.project, conn, self.project_id, self.board, 0, out,
                 sweep_config(self.project))
        return out.getvalue()

    def status(self):
        return self.read("SELECT status FROM tickets")[0][0]

    def ticket(self):
        return self.read("SELECT body, revision FROM tickets")[0]

    def notes(self):
        return [text for (text,) in self.read(
            "SELECT text FROM ticketNotes WHERE kind = 'relandmark'"
            " ORDER BY id")]

    def authors(self):
        return [a for (a,) in self.read(
            "SELECT author FROM ticketRevisions ORDER BY revision")]

    def test_a_moved_file_is_renamed_in_every_section_and_re_readied(self):
        self.file(NAMING_OLD)
        sha = self.move()
        fake = FakeAgent(Commit("the work"), APPROVE)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake), \
                patch("holophyte.review.freshness.critic_admits",
                      return_value=True):
            holophyte.cli.operator.main(self.project, self.board)
        self.assertEqual(self.status(), "needs_spec", out.getvalue())
        self.assertIn("out of date with main", out.getvalue())

        self.sweep()

        body = self.ticket()[0]
        self.assertIn(f"when `{NEW}` runs", body)
        self.assertIn(f"* Anything else in `{NEW}`.", body)
        self.assertIn(f"* Extend `{NEW}` with the thing", body)
        self.assertEqual(self.authors()[-1], "factory")
        self.assertEqual(self.status(), "ready")
        (note,) = self.notes()
        for part in (f"`{OLD}`", f"`{NEW}`", sha):
            self.assertIn(part, note)

    def test_a_verify_module_follows_its_file_and_other_mentions_stay(self):
        self.file(WITH_MODULE)
        self.move()
        self.park(OLD)

        self.sweep()

        body = self.ticket()[0]
        self.assertEqual(self.status(), "ready")
        self.assertIn("python3 -m unittest lotus.features.agents\n", body)
        self.assertIn("(python3 -m unittest lotus.features.agents)&& echo ok",
                      body)
        self.assertIn("python3 -m unittest \"lotus.features.agents\" &&"
                      " echo 'lotus.agents helper'\n", body)
        self.assertIn("python3 -m unittest lotus.features.agents &&"
                      " echo prefix\\ lotus.agents\n", body)
        self.assertNotIn("unittest lotus.agents", body)
        self.assertIn(f"near `{OLD}:2`; plain {OLD} says so.", body)

    def test_a_quoted_verify_path_names_a_shell_special_destination(self):
        dollar = "lotus/features/cost$usd.py"
        self.file(NAMING_OLD.replace(
            "```\necho ok\n```", f'```\nwc -l "{OLD}"\n```'))
        self.move(new=dollar)
        self.park(OLD)

        self.sweep()

        self.assertEqual(self.status(), "ready")
        (line,) = [line for line in self.ticket()[0].splitlines()
                   if line.startswith("wc -l ")]
        argument = subprocess.run(
            ["sh", "-c", "printf %s " + line.removeprefix("wc -l ")],
            capture_output=True, text=True, check=True, env={}).stdout
        self.assertEqual(argument, dollar)

    def test_a_verify_module_the_ticket_declares_new_is_not_a_landmark(self):
        self.file(NAMING_OLD
                  .replace("* Extend", "* Add `tests/test_fresh.py` (new).\n"
                                       "* Extend")
                  .replace("```\necho ok\n```",
                           "```\npython3 -m unittest tests.test_fresh\n```"))
        self.move()
        self.park(OLD)

        self.sweep()

        self.assertEqual(self.status(), "ready")
        self.assertIn(f"* Extend `{NEW}` with the thing", self.ticket()[0])

    def test_a_file_moved_twice_is_named_at_its_final_path(self):
        self.file(NAMING_OLD)
        self.move(OLD, "lotus/interim/agents.py", 60)
        self.move("lotus/interim/agents.py", NEW, 120)
        self.park(OLD)

        self.sweep()

        body = self.ticket()[0]
        self.assertEqual(self.status(), "ready")
        self.assertIn(f"* Extend `{NEW}` with the thing", body)
        self.assertNotIn("interim", body)

    def test_a_deleted_file_keeps_the_park_with_one_note(self):
        self.file(NAMING_OLD)
        self.git("rm", "-q", OLD)
        sha = self.commit("drop the agents module", self.filed + 60)
        self.park(OLD)
        before = self.ticket()

        self.sweep()
        self.sweep()

        self.assertEqual(self.ticket(), before)
        self.assertEqual(self.status(), "needs_spec")
        (note,) = self.notes()
        self.assertIn(f"`{OLD}` was deleted in {sha}", note)

    def test_a_file_copied_and_moved_in_one_commit_keeps_the_park(self):
        self.file(NAMING_OLD)
        copy = self.target / "lotus" / "copy" / "agents.py"
        copy.parent.mkdir(parents=True)
        copy.write_text((self.target / OLD).read_text())
        self.git("add", str(copy))
        self.move()
        self.park(OLD)
        before = self.ticket()

        self.sweep()

        self.assertEqual(self.ticket(), before)
        self.assertEqual(self.status(), "needs_spec")
        (note,) = self.notes()
        self.assertIn("`lotus/copy/agents.py`", note)
        self.assertIn(f"`{NEW}`", note)

    def test_one_unmapped_path_leaves_the_body_alone(self):
        self.file(NAMING_OLD.replace(
            "* Extend", f"* Read `{NEVER}`.\n* Extend"))
        self.move()
        self.park(OLD, NEVER)
        before = self.ticket()

        self.sweep()

        self.assertEqual(self.ticket(), before)
        self.assertEqual(self.status(), "needs_spec")
        (note,) = self.notes()
        self.assertIn(f"`{NEVER}` has no rename since the body was written",
                      note)

    def test_a_move_dated_before_the_body_is_not_followed(self):
        self.file(NAMING_OLD)
        self.move(after=-3600)
        self.park(OLD)
        before = self.ticket()

        self.sweep()

        self.assertEqual(self.ticket(), before)
        self.assertEqual(self.status(), "needs_spec")
        self.assertEqual(self.notes(), [])

    def test_a_failed_history_read_refreshes_nothing(self):
        self.file(NAMING_OLD)
        self.move(after=-3600)
        self.park(OLD)
        before = self.ticket()
        git = holophyte.review.moved_landmarks._git

        def rev_list_of_a_missing_ref(repo, *args):
            if args[0] == "rev-list":
                args = (*args[:-1], "refs/heads/no-such-branch")
            return git(repo, *args)

        with patch.object(holophyte.review.moved_landmarks, "_git",
                          rev_list_of_a_missing_ref):
            out = self.sweep()

        self.assertEqual(self.ticket(), before)
        self.assertEqual(self.status(), "needs_spec")
        self.assertEqual(self.notes(), [])
        self.assertIn("NAT-1 not re-checked: git rev-list failed", out)

    def test_the_refresh_can_be_turned_off(self):
        self.configure(NATIVE + "[loop]\nrefresh_moved_landmarks = false\n")
        self.board = board_for(self.project)
        self.file(NAMING_OLD)
        self.move()
        self.park(OLD)
        before = self.ticket()

        self.sweep()

        self.assertEqual(self.ticket(), before)
        self.assertEqual(self.status(), "needs_spec")
        self.assertEqual(self.notes(), [])

    def test_an_edit_landing_mid_pass_stands(self):
        self.file(NAMING_OLD)
        self.move()
        self.park(OLD)
        edited = NAMING_OLD.replace("The thing, added.", "The thing, edited.")
        refresh = holophyte.review.moved_landmarks.refresh

        def edited_before_the_write(*args, **kwargs):
            refreshed = refresh(*args, **kwargs)
            (revision,) = self.read("SELECT revision FROM tickets")[0]
            store.board.edit_ticket(self.conn, self.project_id, "NAT-1",
                                    edited, revision)
            return refreshed

        factory = self.authors().count("factory")
        with patch.object(holophyte.review.moved_landmarks, "refresh",
                          edited_before_the_write):
            out = self.sweep()

        self.assertEqual(self.ticket()[0], edited)
        self.assertEqual(self.authors().count("factory"), factory)
        self.assertIn("NAT-1 was claimed, edited or parked again", out)

    def add_tools(self):
        (self.target / TOOLS).mkdir()
        for name in ("run.py", "stop.py"):
            (self.target / TOOLS / name).write_text(f"NAME = {name!r}\n")
        self.git("add", TOOLS)
        self.commit("add the tools")

    def claim(self, criterion):
        fake = FakeAgent(Commit("the work"), Reply(APPROVE.text.replace(
            "CRITERION 1:", f"CRITERION {criterion}:")))
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake):
            holophyte.cli.operator.main(self.project, self.board)
        goals = [turn.goal for turn in fake.turns if turn.role == "implement"]
        self.assertTrue(goals, out.getvalue())
        return goals[0]

    def refreshed_tools(self):
        self.add_tools()
        self.file(NAMING_TOOLS)
        sha = self.move(TOOLS, KIT)
        self.park(TOOLS)
        self.sweep()
        self.assertEqual(self.status(), "ready")
        return sha

    def test_a_renamed_directory_rewrites_every_span_under_it(self):
        sha = self.refreshed_tools()

        body = self.ticket()[0]
        self.assertEqual(self.authors()[-1], "factory")
        self.assertIn("when `lotus/kit/stop.py` runs", body)
        self.assertIn("* Extend `lotus/kit` (`lotus/kit/`) through"
                      " `lotus/kit/run.py`.", body)
        self.assertIn("* Add `lotus/kit/fresh.py` (new).", body)
        self.assertNotIn(f"`{TOOLS}", body)
        (note,) = self.notes()
        for part in (f"`{TOOLS}`", f"`{KIT}`", sha):
            self.assertIn(part, note)

    def test_a_directory_split_or_deleted_keeps_the_park_with_one_note(self):
        for moved in ("split", "deleted"):
            with self.subTest(moved):
                self.setUp()
                self.add_tools()
                self.file(NAMING_DIR)
                if moved == "split":
                    for name, place in (("run.py", "lotus/a"),
                                        ("stop.py", "lotus/b")):
                        (self.target / place).mkdir()
                        self.git("mv", f"{TOOLS}/{name}", f"{place}/{name}")
                else:
                    self.git("rm", "-q", "-r", TOOLS)
                sha = self.commit(f"{moved} the tools", self.filed + 60)
                self.park(TOOLS)
                before = self.ticket()

                self.sweep()
                self.sweep()

                self.assertEqual(self.ticket(), before)
                self.assertEqual(self.status(), "needs_spec")
                (note,) = self.notes()
                self.assertIn(
                    f"`{TOOLS}` went to more than one place in {sha}:"
                    " `lotus/a`, `lotus/b`" if moved == "split"
                    else f"`{TOOLS}` was deleted in {sha}", note)

    def test_a_delete_or_move_inside_the_directory_does_not_end_its_chain(self):
        self.add_tools()
        self.file(NAMING_DIR)
        self.git("rm", "-q", f"{TOOLS}/stop.py")
        self.git("mv", f"{TOOLS}/run.py", f"{TOOLS}/runner.py")
        self.commit("trim the tools", self.filed + 30)
        sha = self.move(TOOLS, KIT)
        self.park(TOOLS)

        self.sweep()

        self.assertEqual(self.status(), "ready")
        self.assertIn(f"* Extend `{KIT}`.", self.ticket()[0])
        (note,) = self.notes()
        self.assertIn(f"`{TOOLS}` → `{KIT}` (renamed to `{KIT}` in {sha})",
                      note)

    def test_the_implementer_of_a_refreshed_ticket_is_told_the_new_paths(self):
        sha = self.refreshed_tools()

        goal = self.claim("C2")

        self.assertIn(f"\n{holophyte.review.moved_landmarks.RENAMED_BRIEF}\n",
                      goal)
        (bullet,) = [line for line in goal.splitlines()
                     if line.startswith(f"* `{TOOLS}`")]
        for part in (f"`{KIT}`", sha):
            self.assertIn(part, bullet)

    def test_a_ticket_edited_after_its_refresh_or_never_refreshed_has_no_brief(self):
        for refreshed in (True, False):
            with self.subTest(refreshed=refreshed):
                self.setUp()
                if refreshed:
                    self.refreshed_tools()
                    body, revision = self.ticket()
                    store.board.edit_ticket(
                        self.conn, self.project_id, "NAT-1",
                        body.replace("The thing, added.", "The thing, edited."),
                        revision, author="cli")
                    self.assertEqual(self.status(), "ready")
                else:
                    self.add_tools()
                    self.file(NAMING_TOOLS)

                goal = self.claim("C2" if refreshed else "C1")

                self.assertNotIn("Main renamed paths", goal)
