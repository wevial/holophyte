"""Wiring contract: review rounds as structured findings rows (state-model §2).

Each review the loop runs is persisted as a `reviewRounds` row — verdict,
findings with a path/line/severity/message, the verify result the reviewer was
briefed with, and the fingerprint §6 compares rounds by — instead of surviving
only as prose in FINDINGS.md. These tests drive `main()` over a real throwaway
repo with only the agent turns faked, so the oracle is the stored row and not
the factory's view of it, and the expected fingerprints are computed from
findings written out by hand here rather than from the ones the parser
produced.

Run: python3 -m unittest discover -s tests -p 'test_wiring*' -v
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.review  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above


class StubProvider:
    """The provider seam `main()` drives, plus the module `run_task` imports."""

    TEAM = "team-under-test"
    team = TEAM  # the `Provider` protocol's spelling

    def __init__(self, *tasks):
        self.queue = list(tasks)
        self.states = []
        self.comments = []

    def claim_next(self, skip=(), order="identifier"):
        """The first queued task the loop has not already refused.

        `skip` is honored rather than ignored because the real provider hands
        back the *same* head-of-queue ticket on every ask; a stub that popped
        blindly would let a loop that cannot skip look like one that can.
        """
        for i, task in enumerate(self.queue):
            if task["id"] not in skip:
                return self.queue.pop(i)
        return None

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, task_id, body):
        self.comments.append((task_id, body))


class ReviewRoundRowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.target.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.target / "README.md").write_text("base\n")
        # The test the scripted approvals name as their witness: since KO-215
        # the loop checks a named test exists in the worktree.
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text(
            "def test_it_works():\n    pass\n")
        (self.target / "tests" / "test_store.py").write_text(
            "def test_fingerprint():\n    pass\n\n\n"
            "def test_carry_over():\n    pass\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

        self.db = root / "repo.holophyte.db"
        # The `Target` the loop is handed, with the store and the worktrees
        # placed by hand: outside the target, never a file in it.
        self.tgt = holophyte.target.Target(
            path=self.target, holo_dir=root, store_path=self.db,
            config_path=root / "config.toml",
            worktrees=root / "repo.worktrees")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def loop(self, *replies, criteria=("Given the thing, when it runs, "
                                       "then it works",)):
        """Run the loop over one task, answering each review turn in order."""
        turns = []
        replies = list(replies)

        def fake_agent(target, role, goal, cwd, *, base_sha=None,
                       candidate_sha=None, timeout=None):
            turns.append(role)
            if role != "implement":
                return replies.pop(0)
            n = sum(1 for turn in turns if turn == "implement")
            (Path(cwd) / f"change{n}.txt").write_text(f"work {n}\n")
            self.git("add", "-A", cwd=cwd)
            self.git("commit", "-q", "-m", f"work {n}", cwd=cwd)
            return f"committed work {n}"

        provider = StubProvider(
            {"id": "KO-130", "issue_id": "iss-130", "title": "add a thing",
             "verify": "echo ok", "budget_min": 5, "contracts": [],
             "criteria": list(criteria)})
        with patch.dict(sys.modules, {"linear_provider": provider}):
            with patch.object(holophyte.loop, "agent", fake_agent):
                holophyte.loop.main(self.tgt, provider)
        return provider

    def rounds(self):
        """The stored rounds, read over a connection the factory never had."""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        rows = conn.execute(
            "SELECT round, verdict, findings, findingsFingerprint,"
            " reviewerModel, verificationResults, startedAt, endedAt"
            " FROM reviewRounds ORDER BY round").fetchall()
        return [dict(zip(("round", "verdict", "findings", "fingerprint",
                          "model", "verification", "started", "ended"), row))
                for row in rows]

    # --- structured findings ---------------------------------------------

    def test_a_request_changes_round_stores_its_findings_as_rows(self):
        """§2's finding shape, extracted from the output format that exists
        today: the cited path and line, the message that explains it, and a
        severity that only an explicit marker moves off p2."""
        self.loop(
            "Two blockers.\n\n"
            "- [BLOCKER] factory.py:512 the round is recorded after the break,\n"
            "  so an approving round is never stored.\n"
            "- store.py:1180 the docstring names three columns and lists four.\n"
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "\nVERDICT: REQUEST_CHANGES",
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "VERDICT: APPROVE")

        first = self.rounds()[0]
        self.assertEqual((first["round"], first["verdict"]),
                         (1, "changes_requested"))
        findings = json.loads(first["findings"])
        self.assertEqual(
            [(f["path"], f.get("line"), f["severity"]) for f in findings],
            [("factory.py", 512, "p0"), ("store.py", 1180, "p2")])
        self.assertIn("never stored", findings[0]["message"])
        self.assertIn("lists four", findings[1]["message"])
        # The stored digest is the one §6 will compare against, so it is held
        # against findings written out here rather than against the row's own.
        self.assertEqual(first["fingerprint"], store.findings_fingerprint([
            {"path": "factory.py", "line": 512, "severity": "p0"},
            {"path": "store.py", "line": 1180, "severity": "p2"}]))

    def test_a_listed_blocker_citing_no_path_is_kept_beside_a_parsed_one(self):
        """A findings list is not dropped down to the items whose paths this
        parser happens to recognize. The reviewer filed two blockers and one
        names a file with no extension to match on; storing only the other
        would fingerprint the round as a complaint it did not make."""
        self.loop(
            "- factory.py:512 the approving round is never stored.\n"
            "- [BLOCKER] Dockerfile installs build deps into the runtime image.\n"
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "\nVERDICT: REQUEST_CHANGES",
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "VERDICT: APPROVE")

        findings = json.loads(self.rounds()[0]["findings"])
        self.assertEqual([(f["path"], f["severity"]) for f in findings],
                         [("factory.py", "p2"),
                          (holophyte.review.unparsed_path(findings[1]["message"]),
                           "p0")])
        self.assertTrue(
            findings[1]["path"].startswith(holophyte.review.UNPARSED_PATH))
        self.assertIn("build deps into the runtime image",
                      findings[1]["message"])

    def test_two_pathless_findings_are_two_keys_not_one(self):
        """The placeholder path carries a digest of the complaint, so a round
        that filed two findings this parser found no path in is fingerprinted
        as two. Sharing one placeholder would collapse them into a single
        `path:line:severity` key and hash the round to a complaint half its
        size."""
        self.loop(
            "- [BLOCKER] Dockerfile installs build deps into the runtime image.\n"
            "- [BLOCKER] Makefile has no target for the new stage.\n"
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "\nVERDICT: REQUEST_CHANGES",
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "VERDICT: APPROVE")

        findings = json.loads(self.rounds()[0]["findings"])
        self.assertEqual(len(findings), 2)
        self.assertNotEqual(findings[0]["path"], findings[1]["path"])
        self.assertEqual(self.rounds()[0]["fingerprint"],
                         store.findings_fingerprint(findings))
        self.assertNotEqual(
            store.findings_fingerprint(findings),
            store.findings_fingerprint(findings[:1]))

    def test_unrelated_pathless_rounds_do_not_fingerprint_alike(self):
        """Two rounds complaining about different pathless things are two
        rounds. Keying them both to a bare placeholder would make §6 read
        unrelated rounds as the same round twice -- the stuck-review signal
        inverted, on the comparison this task exists to enable."""
        self.loop(
            "- [BLOCKER] Dockerfile installs build deps into the runtime image.\n"
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "\nVERDICT: REQUEST_CHANGES",
            "- [BLOCKER] Makefile has no target for the new stage.\n"
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "\nVERDICT: REQUEST_CHANGES",
            "VERDICT: PASS")

        first, second = self.rounds()[:2]
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(
            store.findings_overlap(json.loads(first["findings"]),
                                   json.loads(second["findings"])), 0)

    def test_a_re_raised_pathless_finding_still_matches_itself(self):
        """The other half of the digest: an unchanged complaint the reviewer
        rewrapped is the same complaint, so the two rounds overlap rather than
        each round reading as new work."""
        self.loop(
            "- [BLOCKER] Dockerfile installs build deps into the runtime image.\n"
            "\nVERDICT: REQUEST_CHANGES",
            "- [BLOCKER] Dockerfile installs build deps\n"
            "  into the runtime image.\n"
            "\nVERDICT: REQUEST_CHANGES",
            "VERDICT: PASS")

        first, second = self.rounds()[:2]
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_unparseable_reviewer_output_is_kept_as_one_raw_finding(self):
        """A reply naming no file is still a round that said something. It is
        recorded whole under a placeholder path — the alternative is a row
        claiming the reviewer found nothing."""
        self.loop("This is sloppy and I do not like it.\n"
                  "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
                  "VERDICT: REQUEST_CHANGES",
                  "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "VERDICT: APPROVE")

        findings = json.loads(self.rounds()[0]["findings"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "p2")
        self.assertIn("This is sloppy and I do not like it.",
                      findings[0]["message"])
        self.assertNotEqual(self.rounds()[0]["fingerprint"],
                            store.EMPTY_FINGERPRINT)

    def test_rounds_repeating_a_finding_overlap_on_their_stored_keys(self):
        """The point of storing findings as rows: two rounds are comparable.
        The second round re-raises one of the first round's complaints and
        adds one, so §6's overlap measure over the stored findings is neither
        0 (nothing shared) nor 1 (the same round twice)."""
        self.loop(
            "- factory.py:512 the approving round is never stored.\n"
            "- store.py:1180 the docstring is stale.\n"
            "\nVERDICT: REQUEST_CHANGES",
            "- factory.py:512 the approving round is still never stored.\n"
            "- review_runner.py:190 the verdict is read twice.\n"
            "\nVERDICT: REQUEST_CHANGES",
            "VERDICT: PASS")

        first, second = (json.loads(r["findings"]) for r in self.rounds()[:2])
        self.assertGreater(store.findings_overlap(first, second), 0)
        self.assertLess(store.findings_overlap(first, second), 1)

    # --- the per-criterion checklist ---------------------------------------

    TWO = ("Given a row, when it is written, then it is fingerprinted",
           "Given existing rows, when the store migrates, then they carry over")

    def test_an_approval_leaving_a_criterion_unwitnessed_is_changes_requested(self):
        """KO-165 was approved with its second criterion unmet. The verdict
        line no longer decides alone: a criterion the reviewer could not
        witness is a finding, and the round is recorded as a request for
        changes naming it."""
        self.loop(
            "Looks fine.\n"
            "CRITERION 1: met \u2014 tests/test_store.py::test_fingerprint\n"
            "CRITERION 2: unwitnessed \u2014 no test covers the carry-over\n"
            "VERDICT: APPROVE",
            "CRITERION 1: met \u2014 tests/test_store.py::test_fingerprint\n"
            "CRITERION 2: met \u2014 tests/test_store.py::test_carry_over\n"
            "VERDICT: APPROVE",
            criteria=self.TWO)

        first, second = self.rounds()
        self.assertEqual(first["verdict"], "changes_requested")
        (finding,) = json.loads(first["findings"])
        self.assertIn("CRITERION 2", finding["message"])
        self.assertIn("no test covers the carry-over", finding["message"])
        self.assertIn("carry over", finding["message"])
        self.assertEqual((finding["path"], finding["line"]), ("criteria", 2))
        self.assertEqual(second["verdict"], "pass")

    def test_an_approval_witnessing_every_criterion_passes_and_merges(self):
        self.loop(
            "CRITERION 1: met \u2014 tests/test_store.py::test_fingerprint\n"
            "CRITERION 2: met \u2014 tests/test_store.py::test_carry_over\n"
            "VERDICT: APPROVE",
            criteria=self.TWO)

        (only,) = self.rounds()
        self.assertEqual(only["verdict"], "pass")
        self.assertEqual(json.loads(only["findings"]), [])
        self.assertIn("change1.txt", self.git("ls-tree", "--name-only", "main"))

    def test_an_approval_with_no_checklist_is_unwitnessed_on_every_criterion(self):
        """A reply that never answered for the criteria has witnessed none of
        them: one finding per criterion, whatever the verdict line says."""
        self.loop("Looks good.\nVERDICT: APPROVE",
                  "CRITERION 1: met \u2014 a\nCRITERION 2: met \u2014 b\n"
                  "VERDICT: APPROVE",
                  criteria=self.TWO)

        first = self.rounds()[0]
        self.assertEqual(first["verdict"], "changes_requested")
        findings = json.loads(first["findings"])
        self.assertEqual([(f["path"], f["line"]) for f in findings],
                         [("criteria", 1), ("criteria", 2)])
        for finding in findings:
            self.assertIn("unwitnessed", finding["message"])

    def test_a_witness_naming_an_existing_class_and_method_stays_met(self):
        """The reviewer named a test precisely and it is there: the check
        changes nothing about the round."""
        self.write_gates_test()
        self.loop(
            "CRITERION 1: met \u2014 tests/test_gates.py::VerifyGateTests::"
            "test_cap_reaps\n"
            "VERDICT: APPROVE")

        (only,) = self.rounds()
        self.assertEqual(only["verdict"], "pass")
        self.assertEqual(json.loads(only["findings"]), [])

    def test_a_witness_naming_a_missing_test_is_unwitnessed(self):
        """A confidently named test that is fiction — the method, the class
        or the whole file absent — does not witness anything: the criterion
        is downgraded and the round asks for changes."""
        cases = {
            "method": ("class VerifyGateTests:\n    def test_other(self):\n"
                       "        pass\n"),
            "class": ("class OtherTests:\n    def test_cap_reaps(self):\n"
                      "        pass\n"),
            "file": None,
        }
        for label, body in cases.items():
            with self.subTest(missing=label):
                self.setUp()
                self.write_gates_test(body)
                self.loop(
                    "CRITERION 1: met \u2014 tests/test_gates.py::"
                    "VerifyGateTests::test_cap_reaps\n"
                    "VERDICT: APPROVE",
                    "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
                    "VERDICT: APPROVE")

                first = self.rounds()[0]
                self.assertEqual(first["verdict"], "changes_requested")
                (finding,) = json.loads(first["findings"])
                self.assertEqual((finding["path"], finding["line"]),
                                 ("criteria", 1))
                self.assertIn("CRITERION 1: unwitnessed \u2014 named test not "
                              "found: tests/test_gates.py::VerifyGateTests::"
                              "test_cap_reaps", finding["message"])

    def test_a_witness_reaching_outside_the_worktree_is_missing(self):
        """A real test that lives outside the candidate worktree — named by
        an absolute path or through `..` — witnesses nothing: only the
        branch under review counts."""
        outside = Path(tempfile.mkdtemp(prefix="outside-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(outside))
        (outside / "named_witness.py").write_text(
            "def test_fiction():\n    pass\n")
        worktree = Path(tempfile.mkdtemp(prefix="worktree-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(worktree))
        escape = Path("..") / outside.name / "named_witness.py"

        for path in (str(outside / "named_witness.py"), str(escape)):
            with self.subTest(path=path):
                references = holophyte.review.test_references(
                    f"{path}::test_fiction")
                self.assertEqual(references, [(path, None, "test_fiction")])
                missing = holophyte.review.missing_witnesses(
                    references, worktree)
                self.assertEqual(len(missing), 1)
                self.assertIn("outside the worktree", missing[0])

    def test_a_witness_naming_a_check_rather_than_a_test_is_left_alone(self):
        self.loop(
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "CRITERION 2: met \u2014 ruff check . passes\n"
            "VERDICT: APPROVE",
            criteria=self.TWO)

        (only,) = self.rounds()
        self.assertEqual(only["verdict"], "pass")
        self.assertEqual(json.loads(only["findings"]), [])

    def write_gates_test(self, body="class VerifyGateTests:\n"
                         "    def test_cap_reaps(self):\n        pass\n"):
        """Commit `tests/test_gates.py` to main with `body`; None leaves the
        file absent."""
        if body is None:
            return
        (self.target / "tests" / "test_gates.py").write_text(body)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "gates test")

    # --- the round's own record ------------------------------------------

    def test_an_approving_round_records_the_verify_result_it_was_given(self):
        """An approval carries no findings — approving prose is not a findings
        list — and the round keeps the verify result the reviewer was briefed
        with and the reviewer route that issued the verdict."""
        self.loop("Looks good.\n"
                  "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
                  "VERDICT: APPROVE")

        (only,) = self.rounds()
        self.assertEqual((only["round"], only["verdict"], only["model"]),
                         (1, "pass", holophyte.config.REVIEW_PROFILE))
        self.assertEqual(json.loads(only["findings"]), [])
        self.assertEqual(only["fingerprint"], store.EMPTY_FINGERPRINT)
        (result,) = json.loads(only["verification"])
        self.assertEqual((result["command"], result["exitCode"]),
                         ("echo ok", 0))
        self.assertIn("ok", result["output"])
        self.assertLessEqual(only["started"], only["ended"])

    def test_the_terminal_adjudication_is_stored_as_a_verdict_only_round(self):
        """The adjudicator is prompted for a verdict and no findings, so its
        round is recorded as one: numbered after the reviews, carrying the
        FAIL and an empty findings list."""
        self.loop("VERDICT: REQUEST_CHANGES", "VERDICT: REQUEST_CHANGES",
                  "The candidate still does not do the thing.\nVERDICT: FAIL")

        rounds = self.rounds()
        self.assertEqual([r["round"] for r in rounds], [1, 2, 3])
        self.assertEqual(rounds[2]["verdict"], "changes_requested")
        self.assertEqual(json.loads(rounds[2]["findings"]), [])

    def test_an_adjudication_naming_no_verdict_keeps_its_reply(self):
        """The one exception to the verdict-only rule: a malformed reply is
        read as FAIL by the loop, and recording it as an empty `error` round
        would throw away the only evidence of why."""
        self.loop("VERDICT: REQUEST_CHANGES", "VERDICT: REQUEST_CHANGES",
                  "I would rather talk about the architecture.")

        (adjudication,) = [r for r in self.rounds() if r["round"] == 3]
        self.assertEqual(adjudication["verdict"], "error")
        (finding,) = json.loads(adjudication["findings"])
        self.assertIn("rather talk about the architecture", finding["message"])


class RoundWriteTests(unittest.TestCase):
    """The store primitive the wiring above stands on."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3")
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "team", Path(tmp.name) / "repo")
        ticket = store.mirror_ticket(self.conn, project, "iss-1", "HOL-1", "a ticket")
        self.run_id = store.claim(self.conn, project, ticket, now=1000)

    def test_a_malformed_finding_stores_no_round_at_all(self):
        """The fingerprint is computed before the insert, so a round whose
        findings cannot be keyed is refused whole rather than stored with a
        digest that means nothing."""
        with self.assertRaises(ValueError):
            store.record_review_round(
                self.conn, self.run_id, 1, "changes_requested", "a-reviewer",
                findings=[{"path": "factory.py", "severity": "urgent"}])

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM reviewRounds").fetchone(),
            (0,))

    def test_findings_that_are_not_json_store_no_round_at_all(self):
        """`findings` and `verificationResults` are JSON columns: a value the
        encoder cannot write, or writes as something no reader can decode,
        is refused as a `ValueError` before the transaction opens."""
        keyed = {"path": "factory.py", "severity": "p0"}
        for label, kwargs in (
            ("bytes message", dict(findings=[{**keyed, "message": b"raw"}])),
            ("set criterion", dict(findings=[{**keyed, "criterion": {"a"}}])),
            ("nan line", dict(findings=[{**keyed, "line": 1,
                                         "message": float("nan")}])),
            ("bytes verify output", dict(verification_results=[
                {"command": "pytest", "exitCode": 1, "output": b"\xff"}])),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    store.record_review_round(
                        self.conn, self.run_id, 1, "changes_requested",
                        "a-reviewer", **kwargs)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM reviewRounds").fetchone(),
            (0,))

    def test_a_document_column_given_prose_stores_no_round_at_all(self):
        """The two document arguments are the schema's object arrays, not any
        iterable: a string or a mapping is refused rather than exploded into
        its characters or its keys and stored as a document nothing wrote."""
        for label, kwargs in (
            ("prose findings", dict(findings="prose")),
            ("prose verification results", dict(verification_results="prose")),
            ("mapping verification results",
             dict(verification_results={"pytest": 0})),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    store.record_review_round(
                        self.conn, self.run_id, 1, "changes_requested",
                        "a-reviewer", **kwargs)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM reviewRounds").fetchone(),
            (0,))


if __name__ == "__main__":
    unittest.main()
